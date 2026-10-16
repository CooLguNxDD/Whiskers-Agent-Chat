"""Full-text search: the FTS5 index, its sync on writes, filters, and both adapters."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from cat_fleet_chat import mcp_app, search
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.waiters import WaitHub

ATTACHMENT = {
    "filename": "quarterly-flamingo-report.pdf",
    "content_type": "application/pdf",
    "size_bytes": 42,
    "storage": "minio",
    "bucket": "cat-fleet-attachments",
    "object_key": "fleet/work/abc/quarterly-flamingo-report.pdf",
    "sha256": "a" * 64,
}

HOSTILE = [
    '"',
    '""',
    "*",
    "**",
    "AND",
    "OR",
    "NOT",
    "NEAR(",
    "NEAR(a b",
    "zebra OR",
    "AND zebra",
    "(zebra",
    "zebra)",
    "title:zebra",
    "^zebra",
    "zebra -crossing",
    '"zebra',
    "zeb*ra*",
    "",
    "   ",
    "\t\n",
    "🦓 zébra 斑马",
    "_",
    "a" * 5_000,
    "zebra " * 1_000,
]


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "search.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


def _ids(result, kind=None):
    return [hit["id"] for hit in result["results"] if kind is None or hit["kind"] == kind]


async def test_post_message_is_indexed_and_highlighted(hub):
    posted = await hub.post_message("fleet", "alice", "the zebra crossing needs a fix")
    await hub.post_message("fleet", "bob", "unrelated chatter")
    result = await hub.search("zebra")
    assert result["mode"] == "bm25"
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["kind"] == "message"
    assert hit["id"] == posted["message"]["id"]
    assert hit["channel"] == "fleet"
    assert hit["author"] == "alice"
    assert hit["created_at"] == posted["message"]["created_at"]
    assert "[zebra]" in hit["snippet"]
    assert hit["score"] > 0


async def test_title_hit_outranks_passing_mention(hub):
    await hub.post_message(
        "fleet",
        "alice",
        "long status update about many things: builds, deploys, reviews, the lint "
        "config, a flaky test, and somewhere in passing the giraffe dashboard too",
    )
    task = await hub.create_task("fleet", "Fix the giraffe dashboard", actor="bob")
    result = await hub.search("giraffe")
    assert [hit["kind"] for hit in result["results"]] == ["task", "message"]
    assert result["results"][0]["id"] == task["task"]["id"]
    assert result["results"][0]["score"] > result["results"][1]["score"]


async def test_task_create_and_edit_keep_the_index_in_sync(hub):
    created = await hub.create_task(
        "fleet", "Paint the shed", description="use the okapi brown", actor="ada"
    )
    task_id = created["task"]["id"]
    found = await hub.search("okapi")
    assert _ids(found, "task") == [task_id]
    assert found["results"][0]["author"] == "ada"

    # A claim changes the indexed author to the assignee, without a duplicate row.
    await hub.claim_task(task_id, "codex")
    claimed = await hub.search("okapi")
    assert _ids(claimed) == [task_id]
    assert claimed["results"][0]["author"] == "codex"

    # A status change also refreshes the entry.
    await hub.update_task_status(task_id, "in_progress", "codex", 2)
    assert _ids(await hub.search("shed", author="codex")) == [task_id]


async def test_store_reindex_replaces_an_edited_task(hub):
    from cat_fleet_chat import store

    created = await hub.create_task("fleet", "Old wombat title", actor="ada")
    task_id = created["task"]["id"]

    async def edit(conn):
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "UPDATE tasks SET title = 'New quokka title', description = 'fresh' WHERE id = ?",
            (task_id,),
        )
        await store.reindex(conn, "task", task_id)
        await conn.commit()

    await hub.db.write(edit)
    assert (await hub.search("wombat"))["results"] == []
    assert _ids(await hub.search("quokka")) == [task_id]


async def test_attachment_add_is_indexed(hub):
    posted = await hub.post_message(
        "fleet", "ada", "see the attached file", attachments_value=[ATTACHMENT]
    )
    attachment_id = posted["message"]["attachments"][0]["id"]
    result = await hub.search("flamingo")
    assert [(hit["kind"], hit["id"]) for hit in result["results"]] == [
        ("attachment", attachment_id)
    ]
    hit = result["results"][0]
    assert hit["author"] == "ada"
    assert hit["channel"] == "fleet"
    assert "[flamingo]" in hit["snippet"]


async def test_kind_filter(hub):
    await hub.post_message("fleet", "ada", "a heron flew by", attachments_value=[
        {**ATTACHMENT, "filename": "heron.png", "content_type": "image/png", "object_key": "fleet/h.png"}
    ])
    await hub.create_task("fleet", "Count the heron", actor="ada")
    assert {hit["kind"] for hit in (await hub.search("heron"))["results"]} == {
        "message",
        "task",
        "attachment",
    }
    assert {hit["kind"] for hit in (await hub.search("heron", kind="task"))["results"]} == {"task"}
    assert {
        hit["kind"] for hit in (await hub.search("heron", kind="message,attachment"))["results"]
    } == {"message", "attachment"}
    assert {
        hit["kind"] for hit in (await hub.search("heron", kind=["task", "message"]))["results"]
    } == {"task", "message"}
    with pytest.raises(HubError) as bad:
        await hub.search("heron", kind="knowledge-base")
    assert bad.value.code == "validation_error"


async def test_channel_filter(hub):
    await hub.create_channel("work")
    await hub.post_message("fleet", "ada", "lemur in fleet")
    in_work = await hub.post_message("work", "ada", "lemur in work")
    result = await hub.search("lemur", channel="work")
    assert _ids(result) == [in_work["message"]["id"]]
    assert result["results"][0]["channel"] == "work"
    with pytest.raises(HubError) as missing:
        await hub.search("lemur", channel="nowhere")
    assert missing.value.status == 404


async def test_author_filter(hub):
    await hub.post_message("fleet", "ada", "walrus spotted")
    by_bob = await hub.post_message("fleet", "bob", "walrus confirmed")
    result = await hub.search("walrus", author="bob")
    assert _ids(result) == [by_bob["message"]["id"]]


async def test_since_filter(hub):
    old = await hub.post_message("fleet", "ada", "ancient narwhal lore")
    fresh = await hub.post_message("fleet", "ada", "breaking narwhal news")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    async def age(conn):
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "UPDATE search_index SET created_at = ? WHERE kind = 'message' AND ref_id = ?",
            (long_ago, old["message"]["id"]),
        )
        await conn.commit()

    await hub.db.write(age)
    assert set(_ids(await hub.search("narwhal"))) == {old["message"]["id"], fresh["message"]["id"]}
    assert _ids(await hub.search("narwhal", since="24h")) == [fresh["message"]["id"]]
    assert _ids(await hub.search("narwhal", since="7d")) == [fresh["message"]["id"]]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _ids(await hub.search("narwhal", since=week_ago)) == [fresh["message"]["id"]]
    assert len((await hub.search("narwhal", since="60d"))["results"]) == 2
    with pytest.raises(HubError) as bad:
        await hub.search("narwhal", since="last tuesday")
    assert bad.value.code == "validation_error"


def test_since_parsing_is_utc_text():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert search.search_since("24h", now) == "2026-04-30T12:00:00+00:00"
    assert search.search_since("2w", now) == "2026-04-17T12:00:00+00:00"
    assert search.search_since("2026-04-01T10:00:00Z") == "2026-04-01T10:00:00+00:00"
    assert search.search_since("2026-04-01T03:00:00-07:00") == "2026-04-01T10:00:00+00:00"
    assert search.search_since("2026-04-01") == "2026-04-01T00:00:00+00:00"
    assert search.search_since(None) is None


def test_build_match_quotes_every_term():
    assert search.build_match("zebra crossing") == '"zebra" "crossing"'
    assert search.build_match('zeb* "NEAR(x' ) == '"zeb"* "x"'
    assert search.build_match("zebra OR AND") == '"zebra"'
    assert search.build_match("AND OR NOT") is None
    assert search.build_match('"  * ') is None
    assert search.build_match("") is None
    assert search.build_match(" ".join(f"w{i}" for i in range(100))).count('"') == 2 * search.TERMS_MAX


async def test_hostile_queries_never_raise(hub):
    await hub.post_message("fleet", "ada", "zebra crossing")
    for query in HOSTILE:
        result = await hub.search(query)
        assert isinstance(result["results"], list), query
    # Words still match when wrapped in operators and punctuation.
    for query in ["zebra OR", "(zebra", '"zebra', "zebra!", "zeb*", "ZEBRA"]:
        assert len((await hub.search(query))["results"]) == 1, query
    assert (await hub.search("   "))["results"] == []


async def test_unicode_and_diacritics(hub):
    await hub.post_message("fleet", "ada", "Le café crème est prêt 斑马")
    assert len((await hub.search("cafe creme"))["results"]) == 1
    assert len((await hub.search("CAFÉ"))["results"]) == 1
    assert len((await hub.search("斑马"))["results"]) == 1


async def test_pagination(hub):
    posted = [
        (await hub.post_message("fleet", "ada", f"capybara note number {i}"))["message"]["id"]
        for i in range(7)
    ]
    first = await hub.search("capybara", limit=3)
    assert len(first["results"]) == 3
    assert first["total_estimate"] == 7
    assert first["next_offset"] == 3
    second = await hub.search("capybara", limit=3, offset=first["next_offset"])
    third = await hub.search("capybara", limit=3, offset=second["next_offset"])
    assert third["next_offset"] is None
    seen = _ids(first) + _ids(second) + _ids(third)
    assert sorted(seen) == sorted(posted)
    assert len(set(seen)) == 7
    assert (await hub.search("capybara", limit=1000))["limit"] == search.LIMIT_MAX
    assert (await hub.search("capybara"))["limit"] == search.LIMIT_DEFAULT
    with pytest.raises(HubError):
        await hub.search("capybara", offset=-1)


async def test_archived_channel_still_searchable(hub):
    await hub.create_channel("old-work")
    posted = await hub.post_message("old-work", "ada", "the pangolin migration plan")
    await hub.archive_channel("old-work", "ada")
    result = await hub.search("pangolin")
    assert _ids(result) == [posted["message"]["id"]]
    assert _ids(await hub.search("pangolin", channel="old-work")) == [posted["message"]["id"]]


async def test_vector_and_hybrid_modes_are_not_available(hub):
    for mode in ("vector", "hybrid"):
        with pytest.raises(HubError) as refused:
            await hub.search("zebra", mode=mode)
        assert refused.value.code == "not_available"
        assert refused.value.status == 400
        assert mode in refused.value.message
    with pytest.raises(HubError) as unknown:
        await hub.search("zebra", mode="magic")
    assert unknown.value.code == "validation_error"
    assert (await hub.search("zebra", mode="bm25"))["mode"] == "bm25"


async def test_missing_fts5_returns_clear_error(hub):
    hub.db.fts5_available = False
    # Writes keep working without the index.
    await hub.post_message("fleet", "ada", "still works")
    with pytest.raises(HubError) as refused:
        await hub.search("works")
    assert refused.value.status == 501
    assert refused.value.code == "search_unavailable"


async def test_v5_database_backfills_and_remigration_is_idempotent(tmp_path):
    path = tmp_path / "v5.sqlite"
    db = Database(str(path))
    await db.open()
    hub = Hub(db, WaitHub())
    await hub.post_message("fleet", "ada", "indexed before the downgrade")
    await db.close()
    # Turn it back into a v5 database: no index, no v6 row, plus rows written
    # by an old hub that never indexed anything.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE search_index")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 6")
        conn.execute(
            "INSERT INTO messages (channel_id, author, text, created_at) "
            "VALUES (1, 'old', 'legacy armadillo message', '2026-01-01T00:00:00+00:00')"
        )
        message_id = conn.execute("SELECT max(id) FROM messages").fetchone()[0]
        conn.execute(
            "INSERT INTO attachments (message_id, filename, content_type, size_bytes, storage, "
            "bucket, object_key, created_at) VALUES (?, 'armadillo-notes.txt', 'text/plain', 1, "
            "'minio', 'cat-fleet-attachments', 'k', '2026-01-01T00:00:00+00:00')",
            (message_id,),
        )
        conn.execute(
            "INSERT INTO tasks (channel_id, title, description, status, assignee, version, "
            "created_at, updated_at) VALUES (1, 'Armadillo audit', '', 'open', NULL, 1, 't', 't')"
        )
        task_id = conn.execute("SELECT max(id) FROM tasks").fetchone()[0]
        conn.execute(
            "INSERT INTO task_events (task_id, actor, from_status, to_status, note, created_at) "
            "VALUES (?, 'grace', NULL, 'open', '', 't')",
            (task_id,),
        )

    for _ in range(2):  # the second open must not duplicate anything
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        rows = conn.execute("SELECT kind, author FROM search_index ORDER BY rowid").fetchall()
        sources = conn.execute(
            "SELECT (SELECT count(*) FROM messages) + (SELECT count(*) FROM tasks) "
            "+ (SELECT count(*) FROM attachments)"
        ).fetchone()[0]
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert SCHEMA_VERSION >= 6
    assert len(rows) == sources == 4
    assert ("task", "grace") in rows

    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        result = await hub.search("armadillo")
        assert {hit["kind"] for hit in result["results"]} == {"message", "task", "attachment"}
        assert len((await hub.search("downgrade"))["results"]) == 1
    finally:
        await db.close()


async def test_rest_search_shape_and_errors(running):
    client, _ = running
    posted = await client.post(
        "/api/v1/messages", json={"channel": "fleet", "author": "ada", "text": "rest ocelot"}
    )
    assert posted.status_code == 201
    response = await client.get("/api/v1/search", params={"q": "ocelot"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "bm25"
    assert set(body["results"][0]) == {"kind", "id", "channel", "author", "created_at", "snippet", "score"}
    assert body["results"][0]["id"] == posted.json()["message"]["id"]

    repeated = await client.get("/api/v1/search?q=ocelot&kind=task&kind=message")
    assert len(repeated.json()["results"]) == 1

    vector = await client.get("/api/v1/search", params={"q": "ocelot", "mode": "vector"})
    assert vector.status_code == 400
    assert vector.json()["error"]["code"] == "not_available"
    assert "vector" in vector.json()["error"]["message"]

    missing = await client.get("/api/v1/search")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "validation_error"

    hostile = await client.get("/api/v1/search", params={"q": 'NEAR( "*'})
    assert hostile.status_code == 200
    assert hostile.json()["results"] == []


async def test_rest_search_requires_the_bearer_token(authed):
    client, _ = authed
    denied = await client.get("/api/v1/search", params={"q": "anything"})
    assert denied.status_code == 401
    allowed = await client.get(
        "/api/v1/search",
        params={"q": "anything"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert allowed.status_code == 200


async def test_mcp_fleet_search_registered_and_matches_rest(running):
    client, _ = running
    for text in ("mcp tapir one", "mcp tapir two", "no match here"):
        await client.post("/api/v1/messages", json={"channel": "fleet", "author": "ada", "text": text})
    await client.post(
        "/api/v1/tasks", json={"channel": "fleet", "title": "Feed the tapir", "actor": "ada"}
    )

    tools = await mcp_app.build_mcp().list_tools()
    assert "fleet_search" in {tool.name for tool in tools}

    via_rest = (await client.get("/api/v1/search", params={"q": "tapir"})).json()
    via_mcp = await mcp_app.fleet_search("tapir")
    assert via_mcp == via_rest
    assert len(via_mcp["results"]) == 3

    filtered = await mcp_app.fleet_search("tapir", kind=["task"], channel="fleet", limit=5)
    assert [hit["kind"] for hit in filtered["results"]] == ["task"]

    refused = await mcp_app.fleet_search("tapir", mode="hybrid")
    assert refused["status"] == "error"
    assert refused["error"] == "not_available"
