"""Knowledge facts: dedupe, validation, paging, search, LEARNED feed posts, JSONL, REST, MCP, schema."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastmcp import Client

from cat_fleet_chat import knowledge, mcp_app
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import chat_payloads, event_line, matches

# The shape fleet.py writes to goal/<project>/_knowledge-inbox.jsonl. Synthetic content.
INBOX_LINE = json.dumps(
    {
        "ts": "2026-01-02T03:04:05Z",
        "project": "demo",
        "run": "2026-01-01-0000-demo-run",
        "step": "S01-example",
        "author": "w-opus",
        "topic": "gotchas",
        "fact": "Example widgets must be flushed before the frobnicator restarts.",
        "extra": {"ignored": True},
    }
)


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "knowledge.sqlite"))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        hub.feed_post_hooks.append(hub.knowledge_from_feed_post)
        yield hub
    finally:
        await db.close()


async def all_events(hub: Hub) -> list[dict]:
    return await hub.events_after(0, channel=None, limit=1_000)


async def until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# ---- pure helpers -----------------------------------------------------------


def test_normalize_fact_collapses_case_whitespace_and_punctuation():
    base = knowledge.normalize_fact("SQLite FTS5 needs a rebuild after bulk import")
    for variant in (
        "sqlite fts5 needs a rebuild after bulk import",
        "  SQLite   FTS5 needs\ta rebuild\nafter bulk import.  ",
        "SQLITE FTS5 NEEDS A REBUILD AFTER BULK IMPORT!!",
        "SQLite FTS5 needs a rebuild after bulk import ;",
    ):
        assert knowledge.normalize_fact(variant) == base, variant
    assert knowledge.normalize_fact("a rebuild after bulk import") != base


def test_parse_learned_cases():
    assert knowledge.parse_learned("LEARNED: FTS5 needs a rebuild") == (None, "FTS5 needs a rebuild")
    assert knowledge.parse_learned("learned:   lower case works") == (None, "lower case works")
    assert knowledge.parse_learned("Learned : spaced colon") == (None, "spaced colon")
    assert knowledge.parse_learned("LEARNED: [SQLite] FTS5 needs a rebuild") == ("sqlite", "FTS5 needs a rebuild")
    assert knowledge.parse_learned("\n\n  LEARNED: [ci] after blank lines\nand a second line") == (
        "ci",
        "after blank lines\nand a second line",
    )
    # Not on the first non-blank line, empty fact, no prefix, not text.
    assert knowledge.parse_learned("hello\nLEARNED: too late") is None
    assert knowledge.parse_learned("LEARNED:   ") is None
    assert knowledge.parse_learned("LEARNED: [topic]") is None
    assert knowledge.parse_learned("I LEARNED: nothing") is None
    assert knowledge.parse_learned(None) is None

    found = knowledge.learned_from_post(
        {"body": "LEARNED: [sqlite] vacuum after deletes #demo #run-2026-01-01-x #step-S01", "hashtags": ["demo", "run", "step"]}
    )
    assert found == {
        "project": "demo",
        "topic": "sqlite",
        "fact": "vacuum after deletes",
        "source_run": "2026-01-01-x",
        "source_step": "S01",
    }
    plain = knowledge.learned_from_post({"body": "LEARNED: no tags here", "hashtags": []})
    assert plain["project"] == "general" and plain["topic"] is None


# ---- hub --------------------------------------------------------------------


async def test_add_and_dedupe(hub):
    a = await hub.knowledge_add("demo", "SQLite FTS5 needs a rebuild after bulk import", topic="sqlite", author="alice")
    assert a["created"] is True
    fact = a["fact"]
    assert fact["project"] == "demo" and fact["topic"] == "sqlite" and fact["status"] == "active"
    assert fact["author"] == "alice" and fact["source_run"] is None
    for text in (
        "  sqlite fts5 needs a rebuild after bulk import. ",
        "SQLITE   FTS5 NEEDS A REBUILD AFTER BULK IMPORT!",
    ):
        again = await hub.knowledge_add("demo", text, topic="other", author="bob")
        assert again == {"fact": fact, "created": False}
    # Another project is another fact.
    other = await hub.knowledge_add("Elsewhere", "SQLite FTS5 needs a rebuild after bulk import", author="bob")
    assert other["created"] is True and other["fact"]["project"] == "elsewhere"
    assert other["fact"]["topic"] == "general"
    assert len((await hub.knowledge_list())["facts"]) == 2


async def test_validation_errors(hub):
    bad = [
        dict(project="", fact="x", author="ada"),
        dict(project="has space", fact="x", author="ada"),
        dict(project="demo", fact="   ", author="ada"),
        dict(project="demo", fact="x" * (knowledge.FACT_MAX + 1), author="ada"),
        dict(project="demo", fact=42, author="ada"),
        dict(project="demo", fact="x", author="not a handle"),
        dict(project="demo", fact="x", author="ada", topic="bad topic!"),
        dict(project="demo", fact="x", author="ada", source_run="has spaces"),
    ]
    for kwargs in bad:
        with pytest.raises(HubError) as info:
            await hub.knowledge_add(**kwargs)
        assert info.value.code == "validation_error", kwargs
    ok = await hub.knowledge_add("demo", "x" * knowledge.FACT_MAX, author="ada")
    with pytest.raises(HubError) as info:
        await hub.knowledge_set_status("ada", ok["fact"]["id"], "deleted")
    assert info.value.details["allowed"] == list(knowledge.STATUSES)
    with pytest.raises(HubError) as info:
        await hub.knowledge_set_status("ada", 9_999, "rejected")
    assert info.value.code == "not_found"
    with pytest.raises(HubError):
        await hub.knowledge_list(status="gone")
    with pytest.raises(HubError):
        await hub.knowledge_list(cursor="garbage")


async def test_list_filters_topics_and_cursor_paging(hub):
    ids = []
    for i in range(7):
        added = await hub.knowledge_add(
            "demo" if i % 2 == 0 else "other",
            f"fact number {i}",
            topic="ci" if i < 3 else "sqlite",
            author="ada" if i < 5 else "bob",
        )
        ids.append(added["fact"]["id"])

    pages, cursor = [], None
    while True:
        page = await hub.knowledge_list(cursor=cursor, limit=3)
        pages.append([fact["id"] for fact in page["facts"]])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert pages == [ids[6:3:-1], ids[3:0:-1], ids[0:1]]
    # A cursor stays stable when newer facts arrive.
    first = await hub.knowledge_list(limit=3)
    await hub.knowledge_add("demo", "a newer fact", author="cy")
    second = await hub.knowledge_list(cursor=first["next_cursor"], limit=3)
    assert [fact["id"] for fact in second["facts"]] == ids[3:0:-1]

    demo = await hub.knowledge_list(project="demo")
    assert all(fact["project"] == "demo" for fact in demo["facts"]) and len(demo["facts"]) == 5
    assert [f["id"] for f in (await hub.knowledge_list(topic="ci"))["facts"]] == ids[2::-1]
    assert [f["id"] for f in (await hub.knowledge_list(author="bob"))["facts"]] == [ids[6], ids[5]]
    await hub.knowledge_set_status("ada", ids[0], "superseded")
    assert [f["id"] for f in (await hub.knowledge_list(status="superseded"))["facts"]] == [ids[0]]

    topics = await hub.knowledge_topics("demo")
    assert topics == {"project": "demo", "topics": [{"topic": "sqlite", "count": 2}, {"topic": "ci", "count": 1}, {"topic": "general", "count": 1}]}


async def test_search_by_kind_knowledge_finds_fact(hub):
    added = await hub.knowledge_add("demo", "SQLite FTS5 needs a rebuild after bulk import", topic="sqlite", author="alice")
    await hub.post_message("fleet", "ada", "a rebuild of the portal is scheduled")
    found = await hub.search("rebuild", kind="knowledge")
    assert [hit["id"] for hit in found["results"]] == [added["fact"]["id"]]
    hit = found["results"][0]
    assert hit["kind"] == "knowledge" and hit["project"] == "demo" and hit["topic"] == "sqlite"
    assert hit["author"] == "alice" and hit["channel"] is None and "[rebuild]" in hit["snippet"]
    # No kind filter returns both, and other kinds keep their shape.
    both = await hub.search("rebuild")
    kinds = {hit["kind"]: hit for hit in both["results"]}
    assert set(kinds) == {"knowledge", "message"}
    assert "project" not in kinds["message"]
    # Project and topic words find the fact too.
    assert (await hub.search("sqlite", kind="knowledge"))["results"]
    assert (await hub.search("demo", kind=["knowledge"]))["results"]


async def test_status_change_hides_fact_from_search(hub):
    added = await hub.knowledge_add("demo", "Widgets wobble when frozen", author="alice")
    fact_id = added["fact"]["id"]
    assert len((await hub.search("wobble", kind="knowledge"))["results"]) == 1
    changed = await hub.knowledge_set_status("bob", fact_id, "rejected")
    assert changed["fact"]["status"] == "rejected" and changed["previous_status"] == "active"
    assert changed["idempotent"] is False
    assert (await hub.search("wobble"))["results"] == []
    assert (await hub.knowledge_set_status("bob", fact_id, "rejected"))["idempotent"] is True
    await hub.knowledge_set_status("bob", fact_id, "active")
    assert len((await hub.search("wobble", kind="knowledge"))["results"]) == 1
    await hub.knowledge_set_status("bob", fact_id, "superseded")
    assert (await hub.search("wobble", kind="knowledge"))["results"] == []


async def test_learned_feed_post_creates_exactly_one_fact(hub):
    post = await hub.feed_post(
        "ada", "LEARNED: [sqlite] FTS5 needs a rebuild after bulk import #demo", client_request_id="k-1"
    )
    assert post["post"]["id"]
    facts = (await hub.knowledge_list())["facts"]
    assert len(facts) == 1
    assert facts[0]["project"] == "demo" and facts[0]["topic"] == "sqlite" and facts[0]["author"] == "ada"
    assert facts[0]["fact"] == "FTS5 needs a rebuild after bulk import"
    # A replayed idempotent post neither posts nor learns twice.
    replay = await hub.feed_post(
        "ada", "LEARNED: [sqlite] FTS5 needs a rebuild after bulk import #demo", client_request_id="k-1"
    )
    assert replay == post
    # The same fact posted again dedupes; ordinary posts teach nothing.
    await hub.feed_post("bob", "learned: fts5 needs a rebuild after bulk import. #demo")
    await hub.feed_post("bob", "just chatting about LEARNED: things")
    assert len((await hub.knowledge_list())["facts"]) == 1


async def test_failing_hooks_do_not_fail_the_post(hub):
    async def boom(_result):
        raise RuntimeError("hook exploded")

    hub.feed_post_hooks.insert(0, boom)
    post = await hub.feed_post("ada", "LEARNED: survives a broken hook")
    assert post["post"]["body"].startswith("LEARNED")
    assert len((await hub.knowledge_list())["facts"]) == 1

    # A post longer than a fact may be still posts; the fact is cut to FACT_MAX.
    hub.feed_post_hooks.remove(boom)
    long_post = await hub.feed_post("ada", "LEARNED: " + "y" * 3_000)
    assert long_post["post"]["id"]
    facts = (await hub.knowledge_list())["facts"]
    assert len(facts) == 2 and len(facts[0]["fact"]) == knowledge.FACT_MAX


async def test_jsonl_import_reports_good_duplicate_and_invalid_lines(hub):
    await hub.knowledge_add("demo", "Already known fact", author="ada")
    lines = [
        json.dumps({"project": "demo", "topic": "ci", "fact": "First imported fact"}),
        "",
        json.dumps({"project": "demo", "fact": "already known fact."}),
        "{not json",
        json.dumps(["a", "list"]),
        json.dumps({"project": "demo", "topic": "ci"}),
        json.dumps({"project": "bad project", "fact": "x"}),
        json.dumps({"project": "demo", "fact": "first imported fact"}),
        json.dumps({"project": "demo", "fact": "Second fact", "author": "carol", "unknown": 1}),
    ]
    result = await hub.knowledge_import_jsonl(lines, "importer")
    assert result["added"] == 2 and result["duplicates"] == 2
    assert [item["line"] for item in result["invalid"]] == [4, 5, 6, 7]
    assert all(item["error"] for item in result["invalid"])
    facts = {fact["fact"]: fact for fact in (await hub.knowledge_list())["facts"]}
    assert facts["First imported fact"]["author"] == "importer" and facts["First imported fact"]["topic"] == "ci"
    assert facts["Second fact"]["author"] == "carol" and facts["Second fact"]["topic"] == "general"
    # JSONL text works as well as a list; an all-invalid import changes nothing.
    text = await hub.knowledge_import_jsonl("garbage\n", "importer")
    assert text == {"added": 0, "duplicates": 0, "invalid": [{"line": 1, "error": text["invalid"][0]["error"]}]}


async def test_jsonl_import_reports_lines_json_rejects_without_decode_error(hub):
    # A huge integer raises a plain ValueError and deep nesting a RecursionError,
    # neither a JSONDecodeError; both stay one invalid line, never abort the import.
    lines = [
        '{"project": "demo", "fact": "x", "n": ' + "9" * 5_000 + "}",
        "[" * 10_000,
        json.dumps({"project": "demo", "fact": "Still imported"}),
    ]
    result = await hub.knowledge_import_jsonl(lines, "importer")
    assert result["added"] == 1 and result["duplicates"] == 0
    assert [item["line"] for item in result["invalid"]] == [1, 2]
    assert all(item["error"].startswith("line is not JSON") for item in result["invalid"])


async def test_jsonl_import_cap(hub):
    line = json.dumps({"project": "demo", "fact": "same fact"})
    with pytest.raises(HubError) as info:
        await hub.knowledge_import_jsonl([line] * (knowledge.IMPORT_MAX + 1), "ada")
    assert info.value.code == "validation_error" and info.value.details["max"] == knowledge.IMPORT_MAX
    assert (await hub.knowledge_list())["facts"] == []
    full = await hub.knowledge_import_jsonl([line] * knowledge.IMPORT_MAX, "ada")
    assert full == {"added": 1, "duplicates": knowledge.IMPORT_MAX - 1, "invalid": []}


async def test_export_then_import_round_trip_is_a_no_op(hub, tmp_path):
    await hub.knowledge_add("demo", "Alpha fact", topic="ci", author="ada", source_run="run-1", source_step="S01")
    await hub.knowledge_add("demo", "Beta fact", author="bob")
    rejected = await hub.knowledge_add("demo", "Gamma fact", author="bob")
    await hub.knowledge_add("other", "Delta fact", author="cy")
    await hub.knowledge_set_status("ada", rejected["fact"]["id"], "rejected")

    exported = await hub.knowledge_export_jsonl(project="demo")
    lines = exported["jsonl"].splitlines()
    assert exported["count"] == 2 and len(lines) == 2
    first = json.loads(lines[0])
    assert first == {
        "project": "demo",
        "topic": "ci",
        "fact": "Alpha fact",
        "run": "run-1",
        "step": "S01",
        "author": "ada",
        "ts": first["ts"],
    }
    assert "run" not in json.loads(lines[1])
    assert (await hub.knowledge_export_jsonl(status="all"))["count"] == 4

    before = (await hub.knowledge_list())["facts"]
    again = await hub.knowledge_import_jsonl(lines, "importer")
    assert again == {"added": 0, "duplicates": 2, "invalid": []}
    assert (await hub.knowledge_list())["facts"] == before

    # Into a fresh hub the export restores the same facts and sources.
    db = Database(str(tmp_path / "copy.sqlite"))
    await db.open()
    try:
        copy = Hub(db, WaitHub())
        assert (await copy.knowledge_import_jsonl(exported["jsonl"], "importer"))["added"] == 2
        assert (await copy.knowledge_export_jsonl(project="demo"))["jsonl"] == exported["jsonl"]
    finally:
        await db.close()


async def test_inbox_compatibility_fixture_line(hub):
    result = await hub.knowledge_import_jsonl([INBOX_LINE], "importer")
    assert result == {"added": 1, "duplicates": 0, "invalid": []}
    fact = (await hub.knowledge_list(project="demo"))["facts"][0]
    assert fact["topic"] == "gotchas" and fact["author"] == "w-opus"
    assert fact["source_run"] == "2026-01-01-0000-demo-run" and fact["source_step"] == "S01-example"
    assert fact["created_at"].startswith("2026-01-02T03:04:05")
    line = json.loads((await hub.knowledge_export_jsonl())["jsonl"])
    source = json.loads(INBOX_LINE)
    for key in ("project", "topic", "fact", "run", "step", "author"):
        assert line[key] == source[key], key


async def test_event_emitted_and_formatter_handles_it(hub):
    added = await hub.knowledge_add("demo", "x" * 300, topic="ci", author="ada")
    duplicate = await hub.knowledge_add("demo", "X" * 300, author="bob")
    assert duplicate["created"] is False
    events = [event for event in await all_events(hub) if event["kind"] == "knowledge_fact"]
    assert len(events) == 1
    event = events[0]
    assert event["channel"] is None and event["entity_id"] == added["fact"]["id"]
    assert event["payload"] == {"id": added["fact"]["id"], "project": "demo", "topic": "ci", "author": "ada", "fact": "x" * 120}
    assert "learned" in event_line(event)
    assert chat_payloads([event], "discord") and chat_payloads([event], "slack")
    filtered = {"name": "m", "kinds": [], "channels": [], "mentions": ["ada"], "exclude_authors": []}
    assert matches(filtered, event) is False
    assert matches({**filtered, "mentions": [], "exclude_authors": ["ad*"]}, event) is False
    assert matches({**filtered, "mentions": [], "kinds": ["knowledge_fact"]}, event) is True


# ---- app, REST and MCP ------------------------------------------------------


async def test_webhook_dispatch_survives_knowledge_events(hooked):
    client, _app, sink = hooked
    created = await client.post(
        "/api/v1/webhooks",
        json={"name": "everything", "direction": "out", "url": "http://127.0.0.1:9/x", "format": "discord"},
    )
    assert created.status_code == 201, created.text
    hook = created.json()["webhook"]
    # The app wires the LEARNED hook: this post creates a fact and its event.
    post = await client.post("/api/v1/feed/posts", json={"author": "ada", "body": "LEARNED: [ci] cache the wheels #demo"})
    assert post.status_code == 201, post.text
    await client.post("/api/v1/messages", json={"channel": "fleet", "author": "ada", "text": "after"})

    assert await until(lambda: "after" in json.dumps(sink.bodies()))
    text = json.dumps(sink.bodies(), ensure_ascii=False)
    assert "learned (demo/ci)" in text and "cache the wheels" in text
    fresh = (await client.get(f"/api/v1/webhooks/{hook['id']}")).json()["webhook"]
    assert fresh["enabled"] is True and fresh["failure_count"] == 0
    listed = (await client.get("/api/v1/knowledge/facts", params={"project": "demo"})).json()
    assert [fact["fact"] for fact in listed["facts"]] == ["cache the wheels"]


async def test_rest_knowledge_requires_auth(authed):
    client, _ = authed
    for method, path in (
        ("GET", "/api/v1/knowledge/facts"),
        ("POST", "/api/v1/knowledge/facts"),
        ("PATCH", "/api/v1/knowledge/facts/1"),
        ("GET", "/api/v1/knowledge/topics"),
        ("POST", "/api/v1/knowledge/import"),
        ("GET", "/api/v1/knowledge/export"),
    ):
        response = await client.request(method, path, json={"project": "demo", "fact": "x", "author": "ada"})
        assert response.status_code == 401, path
    ok = await client.get("/api/v1/knowledge/facts", headers={"Authorization": "Bearer test-token"})
    assert ok.status_code == 200 and ok.json() == {"facts": [], "next_cursor": None, "has_more": False}


async def test_rest_knowledge_round_trip(running):
    client, _ = running
    created = await client.post(
        "/api/v1/knowledge/facts",
        json={"project": "demo", "topic": "ci", "fact": "Pin the runner image", "agent_name": "ada", "run": "r-1"},
    )
    assert created.status_code == 201, created.text
    fact = created.json()["fact"]
    assert fact["source_run"] == "r-1" and fact["author"] == "ada"
    again = await client.post("/api/v1/knowledge/facts", json={"project": "demo", "fact": "pin the runner image.", "author": "bob"})
    assert again.status_code == 200 and again.json()["created"] is False
    bad = await client.post("/api/v1/knowledge/facts", json={"project": "demo", "fact": "", "author": "bob"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_error"

    listed = (await client.get("/api/v1/knowledge/facts", params={"project": "demo", "topic": "ci"})).json()
    assert [f["id"] for f in listed["facts"]] == [fact["id"]]
    topics = (await client.get("/api/v1/knowledge/topics", params={"project": "demo"})).json()
    assert topics["topics"] == [{"topic": "ci", "count": 1}]
    hits = (await client.get("/api/v1/search", params={"q": "runner", "kind": "knowledge"})).json()
    assert hits["results"][0]["project"] == "demo"

    ndjson = INBOX_LINE + "\n\nnot json\n"
    imported = await client.post(
        "/api/v1/knowledge/import",
        params={"author": "importer"},
        content=ndjson.encode("utf-8"),
        headers={"Content-Type": "application/x-ndjson"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"added": 1, "duplicates": 0, "invalid": [{"line": 3, "error": imported.json()["invalid"][0]["error"]}]}
    as_json = await client.post("/api/v1/knowledge/import", json={"author": "importer", "lines": [INBOX_LINE]})
    assert as_json.json()["duplicates"] == 1

    exported = await client.get("/api/v1/knowledge/export", params={"project": "demo"})
    assert exported.status_code == 200 and exported.headers["content-type"].startswith("application/x-ndjson")
    assert len(exported.text.splitlines()) == 2
    assert all(json.loads(line)["project"] == "demo" for line in exported.text.splitlines())

    patched = await client.patch(f"/api/v1/knowledge/facts/{fact['id']}", json={"status": "superseded", "actor": "bob"})
    assert patched.status_code == 200 and patched.json()["fact"]["status"] == "superseded"
    missing = await client.patch("/api/v1/knowledge/facts/9999", json={"status": "rejected", "actor": "bob"})
    assert missing.status_code == 404
    assert (await client.get("/api/v1/search", params={"q": "runner"})).json()["results"] == []


async def test_mcp_knowledge_tools_registered_and_work(hub):
    mcp_app.set_hub(hub)
    try:
        async with Client(mcp_app.build_mcp()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {
                "fleet_knowledge_add",
                "fleet_knowledge_list",
                "fleet_knowledge_search",
                "fleet_knowledge_set_status",
                "fleet_knowledge_export",
            } <= names
            added = (
                await client.call_tool(
                    "fleet_knowledge_add",
                    {"project": "demo", "fact": "Retry flaky uploads once", "author": "ada", "topic": "ci"},
                )
            ).structured_content
            assert added["created"] is True
            fact_id = added["fact"]["id"]
            listed = (await client.call_tool("fleet_knowledge_list", {"project": "demo"})).structured_content
            assert [f["id"] for f in listed["facts"]] == [fact_id]
            found = (await client.call_tool("fleet_knowledge_search", {"query": "flaky uploads"})).structured_content
            assert [(h["kind"], h["id"], h["topic"]) for h in found["results"]] == [("knowledge", fact_id, "ci")]
            exported = (await client.call_tool("fleet_knowledge_export", {"project": "demo"})).structured_content
            assert exported["count"] == 1 and json.loads(exported["jsonl"])["fact"] == "Retry flaky uploads once"
            changed = (
                await client.call_tool(
                    "fleet_knowledge_set_status", {"actor": "bob", "fact_id": fact_id, "status": "rejected"}
                )
            ).structured_content
            assert changed["fact"]["status"] == "rejected"
            error = (
                await client.call_tool("fleet_knowledge_add", {"project": "demo", "fact": " ", "author": "ada"})
            ).structured_content
            assert error["status"] == "error" and error["error"] == "validation_error"
    finally:
        mcp_app.set_hub(None)


# ---- schema -----------------------------------------------------------------


async def test_v9_database_upgrades_to_v10(tmp_path):
    path = tmp_path / "v9.sqlite"
    db = Database(str(path))
    await db.open()
    await Hub(db, WaitHub()).post_message("fleet", "ada", "kept message")
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE knowledge_facts")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 10")

    for _ in range(2):
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        columns = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_facts)")]
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert SCHEMA_VERSION >= 10 and versions == list(range(1, SCHEMA_VERSION + 1))
    assert columns == [
        "id", "project", "topic", "fact", "norm", "source_run", "source_step",
        "author", "status", "created_at", "updated_at",
    ]
    assert {"uq_knowledge_facts_norm", "idx_knowledge_facts_topic", "idx_knowledge_facts_status"} <= indexes

    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        await hub.knowledge_add("demo", "Facts work after the upgrade", author="ada")
        assert (await hub.search("upgrade", kind="knowledge"))["results"]
        assert (await hub.search("kept", kind="message"))["results"]
    finally:
        await db.close()

    # The backfill indexes active facts that have no search entry yet, and only those.
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM search_index WHERE kind = 'knowledge'")
        conn.execute(
            "INSERT INTO knowledge_facts (project, topic, fact, norm, author, status, created_at, updated_at) "
            "VALUES ('demo', 'general', 'Rejected backfill fact', 'rejected backfill fact', 'ada', 'rejected', 't', 't')"
        )
    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        assert len((await hub.search("upgrade", kind="knowledge"))["results"]) == 1
        assert (await hub.search("backfill", kind="knowledge"))["results"] == []
    finally:
        await db.close()


# ---- S12-jules-fixes-4 regressions ------------------------------------------


def test_trailing_tags_stripping_does_not_backtrack_on_crafted_input():
    import time

    crafted = "LEARNED: valid fact body" + (" #t" * 500) + " trailing_word"
    start = time.perf_counter()
    parsed = knowledge.learned_from_post({"body": crafted, "hashtags": ["t"]})
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2
    assert parsed is not None
    assert parsed["fact"].startswith("valid fact body")
    assert parsed["fact"].endswith("trailing_word")

    stripped = knowledge.learned_from_post(
        {"body": "LEARNED: clean fact" + (" #tag" * 3_000), "hashtags": ["tag"]}
    )
    assert stripped is not None and stripped["fact"] == "clean fact"


async def test_knowledge_import_batches_database_operations(hub):
    await hub.knowledge_add("demo", "Existing fact 0", author="ada")
    lines = [
        json.dumps({"project": "demo", "topic": "ci", "fact": f"Batch fact {i}"})
        for i in range(150)
    ] + [
        json.dumps({"project": "demo", "topic": "ci", "fact": "Batch fact 0"}),
        json.dumps({"project": "demo", "topic": "ci", "fact": "Existing fact 0"}),
    ]

    statements: list[str] = []
    await hub.db._write.set_trace_callback(statements.append)
    try:
        result = await hub.knowledge_import_jsonl(lines, "importer")
    finally:
        await hub.db._write.set_trace_callback(None)

    assert result == {"added": 150, "duplicates": 2, "invalid": []}
    top_level = [s for s in statements if not s.startswith("--")]
    # Unbatched import would run ~5 top-level SQL statements per item (>750 statements).
    assert len(top_level) < 15, f"expected batched SQL, got {len(top_level)} statements"
    assert len((await hub.search("Batch fact 149", kind="knowledge"))["results"]) == 1
    events = [e for e in await all_events(hub) if e["kind"] == "knowledge_fact"]
    assert len(events) == 151


async def test_search_fetches_knowledge_metadata_in_batch_without_join(hub):
    from cat_fleet_chat import store

    assert not hasattr(store, "_KNOWLEDGE_JOIN")
    await hub.post_message("fleet", "ada", "shared token alpha")
    added = await hub.knowledge_add("demo", "shared token alpha in fact", topic="ops", author="bob")

    queries: list[str] = []
    for conn in hub.db._reads:
        await conn.set_trace_callback(queries.append)
    try:
        msg_only = await hub.search("alpha", kind="message")
        assert [h["kind"] for h in msg_only["results"]] == ["message"]
        assert not any("knowledge_facts" in q for q in queries)

        queries.clear()
        both = await hub.search("alpha")
        by_kind = {h["kind"]: h for h in both["results"]}
        assert by_kind["knowledge"]["id"] == added["fact"]["id"]
        assert by_kind["knowledge"]["project"] == "demo"
        assert by_kind["knowledge"]["topic"] == "ops"
        kf_queries = [q for q in queries if "knowledge_facts" in q]
        assert len(kf_queries) == 1
        assert "LEFT JOIN knowledge_facts" not in kf_queries[0]
    finally:
        for conn in hub.db._reads:
            await conn.set_trace_callback(None)


async def test_knowledge_from_feed_post_catches_and_reports_hub_error(hub, caplog):
    result = {
        "post": {
            "id": 42,
            "author": "not a valid handle!",
            "body": "LEARNED: [ci] rejected because author is invalid #demo",
            "hashtags": ["demo"],
        }
    }
    with caplog.at_level("WARNING", logger="cat_fleet_chat.hub"):
        await hub.knowledge_from_feed_post(result)
    assert "knowledge_error" in result
    assert "author" in result["knowledge_error"]
    assert any("rejected LEARNED fact" in rec.message for rec in caplog.records)
    assert (await hub.knowledge_list())["facts"] == []


async def test_rest_knowledge_import_json_body_cap_and_lines_validation(running, monkeypatch):
    from cat_fleet_chat import rest_app

    client, _ = running
    for bad_lines in (123, {"not": "a list"}, True):
        resp = await client.post(
            "/api/v1/knowledge/import",
            json={"author": "importer", "lines": bad_lines},
        )
        assert resp.status_code == 400, bad_lines
        body = resp.json()["error"]
        assert body["code"] == "validation_error"
        assert body["details"]["field"] == "lines"

    monkeypatch.setattr(rest_app, "KNOWLEDGE_IMPORT_BODY_MAX", 128)
    oversized = json.dumps({"author": "importer", "lines": ["x" * 200]}).encode("utf-8")
    too_big = await client.post(
        "/api/v1/knowledge/import",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"


async def test_rest_knowledge_export_streams_chunks(running, monkeypatch):
    from cat_fleet_chat import store

    client, _ = running
    monkeypatch.setattr(store, "EXPORT_BATCH", 2)
    for i in range(5):
        res = await client.post(
            "/api/v1/knowledge/facts",
            json={"project": "demo", "topic": "ci", "fact": f"Streamed fact {i}", "author": "ada"},
        )
        assert res.status_code == 201

    exported = await client.get("/api/v1/knowledge/export", params={"project": "demo"})
    assert exported.status_code == 200
    lines = [json.loads(line) for line in exported.text.splitlines()]
    assert [item["fact"] for item in lines] == [f"Streamed fact {i}" for i in range(5)]

    bad = await client.get("/api/v1/knowledge/export", params={"status": "bogus"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "validation_error"

