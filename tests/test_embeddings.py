"""Embeddings: packing, providers, the embed queue, vector and hybrid search.

Every test uses a fake deterministic embedder or an in-process HTTP handler;
nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from cat_fleet_chat import embed, mcp_app, search
from cat_fleet_chat.config import Settings
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.waiters import WaitHub

from conftest import _open

# Words that share a concept land on the same axis, so "kitten" is close to
# "feline" although the two never share a token. Other words hash onto the
# remaining axes.
CONCEPTS = {
    "cat": 0, "cats": 0, "kitten": 0, "kittens": 0, "feline": 0, "felines": 0,
    "car": 1, "cars": 1, "automobile": 1, "vehicle": 1, "sedan": 1,
    "rain": 2, "storm": 2, "weather": 2, "drizzle": 2,
    "deploy": 3, "release": 3, "rollout": 3, "ship": 3,
}
DIM = 12


class FakeEmbedder:
    """Deterministic concept-axis embedder. ``fail`` makes every call raise."""

    provider = "fake"
    timeout = 1.0

    def __init__(self, name: str = "fake-model", dim: int = DIM) -> None:
        self.name = name
        self.dim: int | None = None
        self.width = dim
        self.calls: list[list[str]] = []
        self.fail: Exception | None = None

    @property
    def enabled(self) -> bool:
        return True

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * self.width
        for word in text.lower().replace(".", " ").replace(",", " ").split():
            axis = CONCEPTS.get(word)
            if axis is None:
                axis = 4 + sum(map(ord, word)) % (self.width - 4)
                vec[axis] += 0.3
            else:
                vec[axis] += 1.0
        return vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail is not None:
            raise self.fail
        self.dim = self.width
        return [self.vector(text) for text in texts]

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "embed.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


def wire(hub: Hub, embedder=None, **queue_kwargs) -> embed.EmbedQueue:
    embedder = embedder or FakeEmbedder()
    hub.embedder = embedder
    hub.embed_queue = embed.EmbedQueue(hub.db, embedder, **queue_kwargs)
    return hub.embed_queue


def keys(result):
    return [(hit["kind"], hit["id"]) for hit in result["results"]]


async def stored(hub: Hub, model: str | None = None):
    async def op(conn):
        sql = "SELECT kind, ref_id, model, dim, text_hash FROM embeddings"
        params: tuple = ()
        if model is not None:
            sql += " WHERE model = ?"
            params = (model,)
        cursor = await conn.execute(sql + " ORDER BY kind, ref_id, model", params)
        return [tuple(row) for row in await cursor.fetchall()]

    return await hub.db.read(op)


# ---- vectors ---------------------------------------------------------------


def test_pack_unpack_roundtrip_and_normalise():
    vec = [0.5, -1.25, 3.0, 0.0]
    blob = embed.pack(vec)
    assert len(blob) == 4 * len(vec)
    assert embed.unpack(blob) == pytest.approx(vec)
    # float32 little-endian, independent of the host
    assert blob[:4] == bytes.fromhex("0000003f")
    unit = embed.normalize([3.0, 4.0])
    assert unit == pytest.approx([0.6, 0.8])
    assert math.isclose(embed.dot(unit, unit), 1.0)
    assert embed.normalize([0.0, 0.0]) == [0.0, 0.0]
    restored = embed.unpack(embed.pack(embed.normalize([1.0, 2.0, 2.0])))
    assert restored == pytest.approx([1 / 3, 2 / 3, 2 / 3], rel=1e-6)


def test_rrf_fusion_math_and_dedup():
    bm25 = [("message", 1), ("task", 2), ("message", 3)]
    vector = [("task", 2), ("message", 4), ("task", 2)]
    fused = search.rrf_fuse([bm25, vector])
    scores = dict(fused)
    k = search.RRF_K
    assert search.RRF_K == 60
    assert scores[("task", 2)] == pytest.approx(1 / (k + 2) + 1 / (k + 1))
    assert scores[("message", 1)] == pytest.approx(1 / (k + 1))
    assert scores[("message", 4)] == pytest.approx(1 / (k + 2))
    assert scores[("message", 3)] == pytest.approx(1 / (k + 3))
    # one entry per key, best first; ties keep first-seen order
    assert [key for key, _ in fused] == [("task", 2), ("message", 1), ("message", 4), ("message", 3)]
    assert search.fusion_pool(20, 0) == 60
    assert search.fusion_pool(5, 0) == 50
    assert search.fusion_pool(20, 40) == 180


def test_top_k_keeps_best_in_order():
    scored = [(0.1, "a"), (0.9, "b"), (0.5, "c"), (0.9, "d"), (0.3, "e")]
    assert search.top_k(scored, 3) == [(0.9, "b"), (0.9, "d"), (0.5, "c")]
    assert search.top_k([], 3) == []


# ---- queue -----------------------------------------------------------------


async def test_queue_embeds_new_rows_and_skips_unchanged_hash(hub):
    fake = FakeEmbedder()
    queue = wire(hub, fake, batch=2)
    await hub.post_message("fleet", "ada", "the kitten sleeps")
    await hub.create_task("fleet", "Wash the car", actor="ada")
    await hub.post_message("fleet", "bob", "storm warning")
    assert await queue.drain() == 3
    assert len(fake.calls) == 2  # batches of 2 then 1
    rows = await stored(hub)
    assert [(kind, dim) for kind, _, _, dim, _ in rows] == [("message", DIM)] * 2 + [("task", DIM)]

    # Nothing changed: no more provider calls, even after a rescan.
    queue._rescanned = False
    assert await queue.drain() == 0
    assert len(fake.calls) == 2

    # A status change rewrites the index entry with the same text: hash unchanged.
    task_id = rows[-1][1]
    await hub.claim_task(task_id, "codex")
    assert await queue.drain() == 0
    assert len(fake.calls) == 2


async def test_queue_reembeds_edited_text(hub):
    from cat_fleet_chat import store

    fake = FakeEmbedder()
    queue = wire(hub, fake)
    created = await hub.create_task("fleet", "Old title", actor="ada")
    task_id = created["task"]["id"]
    await queue.drain()
    before = await stored(hub)

    async def edit(conn):
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("UPDATE tasks SET title = 'Fresh deploy plan' WHERE id = ?", (task_id,))
        await store.reindex(conn, "task", task_id)
        await conn.commit()

    # Edited behind the queue's back: the startup rescan finds it.
    await hub.db.write(edit)
    queue._rescanned = False
    assert await queue.drain() == 1
    after = await stored(hub)
    assert len(after) == 1 and after[0][4] != before[0][4]
    assert fake.calls[-1] == ["Fresh deploy plan"]


async def test_model_change_reembeds_and_old_rows_are_ignored(hub):
    first = FakeEmbedder("model-a")
    queue = wire(hub, first)
    posted = await hub.post_message("fleet", "ada", "kitten on the keyboard")
    await queue.drain()
    second = FakeEmbedder("model-b")
    queue = wire(hub, second)
    status = await hub.embed_status()
    assert status["model"] == "model-b" and status["indexed"] == 0 and status["pending"] == 1
    assert await queue.drain() == 1
    assert [row[2] for row in await stored(hub)] == ["model-a", "model-b"]
    result = await hub.search("feline", mode="vector")
    assert keys(result) == [("message", posted["message"]["id"])]
    assert (await hub.embed_status())["indexed"] == 1


async def test_dimension_change_is_treated_as_a_new_model(hub):
    narrow = FakeEmbedder("same-name", dim=8)
    queue = wire(hub, narrow)
    await hub.post_message("fleet", "ada", "kitten nap")
    await queue.drain()
    wide = FakeEmbedder("same-name", dim=16)
    queue = wire(hub, wide)
    # The query is 16 wide: stored 8-wide rows are skipped, never a crash.
    assert (await hub.search("cat", mode="vector"))["results"] == []
    assert await queue.drain() == 1
    assert [row[3] for row in await stored(hub)] == [16]
    assert len((await hub.search("cat", mode="vector"))["results"]) == 1


async def test_queue_survives_provider_errors_with_backoff(hub):
    fake = FakeEmbedder()
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) == 4:
            fake.fail = None  # the provider comes back
        await asyncio.sleep(0)

    queue = wire(hub, fake, sleep=fake_sleep, backoff_base=1.0, backoff_max=5.0)
    fake.fail = httpx.ConnectError("refused")
    queue.start()
    try:
        # Writes are not blocked while the embedder is down.
        posted = await asyncio.wait_for(hub.post_message("fleet", "ada", "kitten"), 2)
        for _ in range(500):
            if (await hub.embed_status())["indexed"] == 1:
                break
            await asyncio.sleep(0.01)
        status = await hub.embed_status()
        assert status["indexed"] == 1 and status["pending"] == 0
        assert status["last_error"] is None
        assert delays[:4] == [1.0, 2.0, 4.0, 5.0]
        assert queue.failures == 0
        assert posted["message"]["id"]
    finally:
        await queue.stop()


async def test_queue_reports_safe_last_error(hub):
    fake = FakeEmbedder()
    seen = asyncio.Event()

    async def fake_sleep(seconds):
        seen.set()
        await asyncio.sleep(3600)

    queue = wire(hub, fake, sleep=fake_sleep)
    await hub.post_message("fleet", "ada", "secret plans for the kitten")
    fake.fail = embed.EmbedderError("fake embedder failed: HTTP 500")
    queue.start()
    try:
        await asyncio.wait_for(seen.wait(), 2)
        status = await hub.embed_status()
        assert status["last_error"] == "fake embedder failed: HTTP 500"
        assert status["pending"] == 1
        assert "secret" not in json.dumps(status)
    finally:
        await queue.stop()


async def test_mail_is_never_embedded(hub):
    queue = wire(hub)
    await hub.send_mail("ada", "bob", "private kitten mail", subject="psst")
    await hub.post_message("fleet", "ada", "public kitten")
    await queue.drain()
    assert {row[0] for row in await stored(hub)} == {"message"}
    assert len(await stored(hub)) == 1


# ---- search ----------------------------------------------------------------


async def test_vector_ranks_near_above_far(hub):
    queue = wire(hub)
    near = await hub.post_message("fleet", "ada", "the feline knocked a glass over")
    far = await hub.post_message("fleet", "ada", "the sedan needs new tyres")
    await queue.drain()
    result = await hub.search("kitten", mode="vector")
    assert result["mode"] == "vector"
    assert keys(result)[0] == ("message", near["message"]["id"])
    assert ("message", far["message"]["id"]) in keys(result)
    top, other = result["results"]
    assert top["score"] > other["score"]
    assert -1.0 <= other["score"] <= top["score"] <= 1.0 + 1e-6
    assert set(top) == {"kind", "id", "channel", "author", "created_at", "snippet", "score"}
    assert top["snippet"].startswith("the feline")


async def test_vector_filters(hub):
    queue = wire(hub)
    await hub.create_channel("work")
    in_fleet = await hub.post_message("fleet", "ada", "kitten in fleet")
    in_work = await hub.post_message("work", "bob", "kitten in work")
    task = await hub.create_task("work", "Adopt a kitten", actor="ada")
    old = await hub.post_message("fleet", "cyd", "ancient kitten lore")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    async def age(conn):
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "UPDATE search_index SET created_at = ? WHERE kind = 'message' AND ref_id = ?",
            (long_ago, old["message"]["id"]),
        )
        await conn.commit()

    await hub.db.write(age)
    await queue.drain()
    task_key = ("task", task["task"]["id"])
    assert keys(await hub.search("cat", mode="vector", kind="task")) == [task_key]
    assert set(keys(await hub.search("cat", mode="vector", channel="work"))) == {
        ("message", in_work["message"]["id"]),
        task_key,
    }
    assert keys(await hub.search("cat", mode="vector", author="bob")) == [
        ("message", in_work["message"]["id"])
    ]
    recent = set(keys(await hub.search("cat", mode="vector", since="7d")))
    assert ("message", old["message"]["id"]) not in recent
    assert ("message", in_fleet["message"]["id"]) in recent
    with pytest.raises(HubError) as missing:
        await hub.search("cat", mode="vector", channel="nowhere")
    assert missing.value.status == 404
    paged = await hub.search("cat", mode="vector", limit=2)
    assert paged["total_estimate"] == 4 and paged["next_offset"] == 2
    rest = await hub.search("cat", mode="vector", limit=2, offset=2)
    assert len(set(keys(paged)) | set(keys(rest))) == 4


async def test_hybrid_finds_what_bm25_misses(hub):
    queue = wire(hub)
    semantic = await hub.post_message("fleet", "ada", "a feline is asleep on the server rack")
    literal = await hub.post_message("fleet", "ada", "kitten pictures thread")
    await hub.post_message("fleet", "ada", "the release train leaves at noon")
    await queue.drain()
    bm25 = await hub.search("kitten")
    assert keys(bm25) == [("message", literal["message"]["id"])]
    hybrid = await hub.search("kitten", mode="hybrid")
    assert hybrid["mode"] == "hybrid"
    assert "degraded" not in hybrid
    found = keys(hybrid)
    assert found[0] == ("message", literal["message"]["id"])  # in both rankings
    assert ("message", semantic["message"]["id"]) in found
    assert len(found) == len(set(found))
    k = search.RRF_K
    assert hybrid["results"][0]["score"] == pytest.approx(1 / (k + 1) + 1 / (k + 1))
    assert "[kitten]" in hybrid["results"][0]["snippet"]


async def test_hybrid_pagination_covers_deep_pages(hub):
    queue = wire(hub)
    for i in range(12):
        await hub.post_message("fleet", "ada", f"kitten note {i}")
    await queue.drain()
    seen = []
    offset = 0
    while offset is not None:
        page = await hub.search("kitten", mode="hybrid", limit=5, offset=offset)
        seen += keys(page)
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 12


async def test_off_keeps_not_available(hub):
    await hub.post_message("fleet", "ada", "kitten")
    for mode in ("vector", "hybrid"):
        with pytest.raises(HubError) as refused:
            await hub.search("kitten", mode=mode)
        assert refused.value.code == "not_available"
        assert "CAT_FLEET_EMBED_PROVIDER" in refused.value.message
    status = await hub.embed_status()
    assert status == {
        "provider": "off",
        "model": None,
        "dim": None,
        "indexed": 0,
        "pending": 0,
        "last_error": None,
    }
    queue = embed.EmbedQueue(hub.db, embed.NullEmbedder())
    queue.start()
    assert queue._task is None
    assert await queue.run_once() == 0


async def test_hybrid_degrades_and_vector_fails_clearly(hub):
    fake = FakeEmbedder()
    queue = wire(hub, fake)
    posted = await hub.post_message("fleet", "ada", "kitten on the desk")
    await queue.drain()
    fake.fail = httpx.ConnectTimeout("slow")
    hybrid = await hub.search("kitten", mode="hybrid")
    assert hybrid["mode"] == "hybrid"
    assert hybrid["degraded"] == "bm25"
    assert keys(hybrid) == [("message", posted["message"]["id"])]
    assert hybrid["results"] == (await hub.search("kitten"))["results"]
    with pytest.raises(HubError) as failed:
        await hub.search("kitten", mode="vector")
    assert failed.value.code == "embedder_unavailable"
    assert failed.value.status == 503


async def test_query_embedding_times_out(hub, monkeypatch):
    class Slow(FakeEmbedder):
        async def embed(self, texts):
            await asyncio.sleep(10)

    wire(hub, Slow())
    monkeypatch.setattr(search, "QUERY_EMBED_TIMEOUT", 0.05)
    await hub.post_message("fleet", "ada", "kitten")
    with pytest.raises(HubError) as failed:
        await hub.search("kitten", mode="vector")
    assert failed.value.code == "embedder_unavailable"
    assert "timeout" in failed.value.message
    assert (await hub.search("kitten", mode="hybrid"))["degraded"] == "bm25"


# ---- migration -------------------------------------------------------------


async def test_v8_database_upgrades_to_v9_idempotently(tmp_path):
    path = tmp_path / "v8.sqlite"
    db = Database(str(path))
    await db.open()
    hub = Hub(db, WaitHub())
    await hub.post_message("fleet", "ada", "kitten before the upgrade")
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE embeddings")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 9")

    for _ in range(2):
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        columns = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)")]
        pk = [r[1] for r in sorted(conn.execute("PRAGMA table_info(embeddings)"), key=lambda r: r[5]) if r[5]]
    assert SCHEMA_VERSION >= 9
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert columns == ["kind", "ref_id", "model", "dim", "vector", "text_hash", "created_at"]
    assert pk == ["kind", "ref_id", "model"]

    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        queue = wire(hub)
        assert await queue.drain() == 1
        assert len((await hub.search("cat", mode="vector"))["results"]) == 1
    finally:
        await db.close()


# ---- REST and MCP ----------------------------------------------------------


def _settings(tmp_path, token=None):
    return Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token=token,
        dev_origins=("http://127.0.0.1:3001",),
        webhooks_enabled=False,
    )


async def test_rest_search_status_needs_auth(tmp_path):
    fake = FakeEmbedder()
    async for client, _ in _open(_settings(tmp_path, token="test-token"), embedder=fake):
        denied = await client.get("/api/v1/search/status")
        assert denied.status_code == 401
        allowed = await client.get(
            "/api/v1/search/status", headers={"Authorization": "Bearer test-token"}
        )
        assert allowed.status_code == 200
        body = allowed.json()
        assert set(body) == {"provider", "model", "dim", "indexed", "pending", "last_error"}
        assert body["provider"] == "fake" and body["model"] == "fake-model"


async def test_rest_status_when_off(running):
    client, _ = running
    body = (await client.get("/api/v1/search/status")).json()
    assert body["provider"] == "off" and body["indexed"] == 0
    refused = await mcp_app.fleet_search("anything", mode="vector")
    assert refused["error"] == "not_available"
    assert refused["embed_status"]["provider"] == "off"


async def test_mcp_and_rest_return_the_same_hybrid_hits(tmp_path):
    fake = FakeEmbedder()
    async for client, app in _open(_settings(tmp_path), embedder=fake):
        for text in ("kitten one", "a feline two", "the sedan three"):
            posted = await client.post(
                "/api/v1/messages", json={"channel": "fleet", "author": "ada", "text": text}
            )
            assert posted.status_code == 201
        hub = app.state.hub
        for _ in range(500):
            if (await hub.embed_status())["pending"] == 0:
                break
            await asyncio.sleep(0.01)
        assert (await hub.embed_status())["indexed"] == 3
        via_rest = (await client.get("/api/v1/search", params={"q": "kitten", "mode": "hybrid"})).json()
        via_mcp = await mcp_app.fleet_search("kitten", mode="hybrid")
        assert via_mcp == via_rest
        assert via_rest["mode"] == "hybrid" and len(via_rest["results"]) >= 2
        vector = (await client.get("/api/v1/search", params={"q": "cat", "mode": "vector"})).json()
        assert vector["mode"] == "vector"
        fake.fail = RuntimeError("down")
        failed = await client.get("/api/v1/search", params={"q": "cat", "mode": "vector"})
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "embedder_unavailable"
        mcp_failed = await mcp_app.fleet_search("cat", mode="vector")
        assert mcp_failed["error"] == "embedder_unavailable"
        assert mcp_failed["embed_status"]["provider"] == "fake"


# ---- HTTP providers --------------------------------------------------------


async def test_openai_compatible_request_and_response():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i), 1.0, 0.5]}
            for i, _ in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"object": "list", "data": list(reversed(data)), "model": body["model"]})

    settings = Settings(
        host="127.0.0.1", port=1, db_path="x", token=None, dev_origins=(),
        embed_provider="openai", embed_url="http://embedder.invalid:1234/", embed_model="text-embed",
        embed_api_key="sk-test-key", embed_timeout=3.0,
    )
    embedder = embed.build_embedder(settings, transport=httpx.MockTransport(handler))
    assert isinstance(embedder, embed.OpenAIEmbedder)
    assert "sk-test-key" not in repr(embedder) and "sk-test-key" not in repr(settings)
    vectors = await embedder.embed(["one", "two"])
    assert vectors == [[0.0, 1.0, 0.5], [1.0, 1.0, 0.5]]
    assert embedder.dim == 3
    request = seen[0]
    assert str(request.url) == "http://embedder.invalid:1234/v1/embeddings"
    assert json.loads(request.content) == {"model": "text-embed", "input": ["one", "two"]}
    assert request.headers["Authorization"] == "Bearer sk-test-key"

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="echo: sk-test-key one two")

    bad = embed.OpenAIEmbedder(
        "http://embedder.invalid/v1", "m", api_key="sk-test-key", transport=httpx.MockTransport(broken)
    )
    with pytest.raises(embed.EmbedderError) as failed:
        await bad.embed(["secret text"])
    assert str(failed.value) == "openai embedder failed: HTTP 500"
    await embedder.aclose()
    await bad.aclose()


async def test_content_parts_request_and_response():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert str(request.url) == "http://embedder.invalid/v1/embeddings"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0] for _ in body["input"]]})

    settings = Settings(
        host="127.0.0.1", port=1, db_path="x", token=None, dev_origins=(),
        embed_provider="content-parts", embed_url="http://embedder.invalid", embed_model="parts-model",
    )
    embedder = embed.build_embedder(settings, transport=httpx.MockTransport(handler))
    assert isinstance(embedder, embed.ContentPartsEmbedder)
    assert await embedder.embed(["hello"]) == [[1.0, 0.0]]
    assert seen[0] == {
        "model": "parts-model",
        "input": [{"content": [{"type": "text", "text": "hello"}]}],
    }

    def short(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": []})

    wrong = embed.ContentPartsEmbedder("http://embedder.invalid", "m", transport=httpx.MockTransport(short))
    with pytest.raises(embed.EmbedderError):
        await wrong.embed(["a"])
    await embedder.aclose()
    await wrong.aclose()
    assert isinstance(embed.build_embedder(Settings("127.0.0.1", 1, "x", None, ())), embed.NullEmbedder)


def test_load_settings_reads_embed_env(monkeypatch):
    from cat_fleet_chat.config import load_settings

    monkeypatch.setenv("CAT_FLEET_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("CAT_FLEET_EMBED_URL", "http://embedder.invalid")
    monkeypatch.setenv("CAT_FLEET_EMBED_MODEL", "m")
    monkeypatch.setenv("CAT_FLEET_EMBED_BATCH", "4")
    monkeypatch.setenv("CAT_FLEET_EMBED_API_KEY", "sk-hidden")
    settings = load_settings()
    assert (settings.embed_provider, settings.embed_batch, settings.embed_timeout) == ("openai", 4, 20.0)
    assert "sk-hidden" not in repr(settings)
    monkeypatch.setenv("CAT_FLEET_EMBED_PROVIDER", "bogus")
    with pytest.raises(SystemExit):
        load_settings()
    monkeypatch.delenv("CAT_FLEET_EMBED_PROVIDER")
    assert load_settings().embed_provider == "off"
