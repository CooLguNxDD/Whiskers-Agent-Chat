"""Feed reactions: +1 / -1 votes, emoji kinds, counts on shaped posts, REST, MCP, events, schema v12."""

from __future__ import annotations

import sqlite3

import pytest
from fastmcp import Client

from cat_fleet_chat import feed, mcp_app, store
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import chat_payloads, event_actor, event_line


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "reactions.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


def pid(result: dict) -> int:
    return result["post"]["id"]


async def reaction_events(hub: Hub) -> list[dict]:
    events = await hub.events_after(0, channel=None, limit=1_000)
    return [e for e in events if e["kind"].startswith("reaction_")]


async def stored_rows(hub: Hub, post_id: int) -> list[tuple[str, str]]:
    async def op(conn):
        return await store.reaction_rows(conn, post_id)

    return [(row["handle"], row["kind"]) for row in await hub.db.read(op)]


# ---- semantics --------------------------------------------------------------


def test_schema_version_is_12():
    assert SCHEMA_VERSION == 12


async def test_react_is_idempotent_and_unique_per_post_handle_kind(hub):
    post = pid(await hub.feed_post("ada", "react to me"))
    first = await hub.react("bob", post, "+1")
    assert first["idempotent"] is False and first["reactions"] == {"+1": 1} and first["score"] == 1
    assert first["mine"] == ["+1"] and first["replaced"] == []
    again = await hub.react("bob", post, "+1")
    assert again["idempotent"] is True and again["reactions"] == {"+1": 1}
    assert await stored_rows(hub, post) == [("bob", "+1")]

    async def duplicate(conn):
        await conn.execute(
            "INSERT INTO reactions (post_id, handle, kind, created_at) VALUES (?, 'bob', '+1', 't')", (post,)
        )

    with pytest.raises(sqlite3.IntegrityError):
        await hub._txn(duplicate)


async def test_votes_exclude_each_other(hub):
    post = pid(await hub.feed_post("ada", "vote"))
    await hub.react("bob", post, "+1")
    down = await hub.react("bob", post, "-1")
    assert down["replaced"] == ["+1"] and down["reactions"] == {"-1": 1} and down["score"] == -1
    assert down["mine"] == ["-1"]
    back = await hub.react("bob", post, "+1")
    assert back["replaced"] == ["-1"] and back["reactions"] == {"+1": 1}
    assert await stored_rows(hub, post) == [("bob", "+1")]


async def test_emoji_kinds_are_independent(hub):
    post = pid(await hub.feed_post("ada", "party"))
    await hub.react("bob", post, "+1")
    await hub.react("bob", post, "heart")
    result = await hub.react("bob", post, "rocket")
    assert result["replaced"] == []
    assert result["reactions"] == {"+1": 1, "heart": 1, "rocket": 1}
    assert result["score"] == 1 and sorted(result["mine"]) == ["+1", "heart", "rocket"]
    # A vote change leaves emoji alone.
    voted = await hub.react("bob", post, "-1")
    assert voted["reactions"] == {"-1": 1, "heart": 1, "rocket": 1}


async def test_unreact_removes_one_kind_idempotently(hub):
    post = pid(await hub.feed_post("ada", "undo"))
    await hub.react("bob", post, "+1")
    await hub.react("bob", post, "eyes")
    gone = await hub.unreact("bob", post, "eyes")
    assert gone["idempotent"] is False and gone["reactions"] == {"+1": 1} and gone["mine"] == ["+1"]
    again = await hub.unreact("bob", post, "eyes")
    assert again["idempotent"] is True and again["reactions"] == {"+1": 1}
    never = await hub.unreact("cy", post, "tada")
    assert never["idempotent"] is True


async def test_invalid_kind_is_refused_with_allowed_list(hub):
    post = pid(await hub.feed_post("ada", "x"))
    for bad in ("👍", "+2", "", None, "HEARTS"):
        with pytest.raises(HubError) as caught:
            await hub.react("bob", post, bad)
        assert caught.value.code == "validation_error" and caught.value.status == 400
        assert caught.value.details == {"field": "kind", "allowed": list(feed.REACTION_KINDS)}
    with pytest.raises(HubError) as caught:
        await hub.unreact("bob", post, "nope")
    assert caught.value.code == "validation_error"
    assert await stored_rows(hub, post) == []


async def test_missing_or_deleted_post_is_not_found(hub):
    with pytest.raises(HubError) as caught:
        await hub.react("bob", 9_999, "+1")
    assert caught.value.code == "not_found" and caught.value.status == 404
    with pytest.raises(HubError) as caught:
        await hub.list_reactions(9_999)
    assert caught.value.code == "not_found"
    post = pid(await hub.feed_post("ada", "soon gone"))
    await hub.feed_delete("ada", post)
    with pytest.raises(HubError) as caught:
        await hub.react("bob", post, "heart")
    assert caught.value.code == "not_found" and caught.value.status == 404


async def test_two_handles_accumulate_and_list_shows_handles(hub):
    post = pid(await hub.feed_post("ada", "popular"))
    await hub.react("bob", post, "+1")
    await hub.react("cy", post, "+1")
    await hub.react("dee", post, "-1")
    await hub.react("cy", post, "laugh")
    listed = await hub.list_reactions(post)
    assert listed["post_id"] == post
    assert listed["reactions"] == {"+1": 2, "-1": 1, "laugh": 1}
    assert listed["handles"] == {"+1": ["bob", "cy"], "-1": ["dee"], "laugh": ["cy"]}
    assert listed["score"] == 1


async def test_reacting_to_a_repost_lands_on_the_original(hub):
    original = pid(await hub.feed_post("ada", "original"))
    repost = pid(await hub.feed_repost("bob", original))
    result = await hub.react("cy", repost, "tada")
    assert result["post_id"] == original
    assert (await hub.list_reactions(original))["reactions"] == {"tada": 1}


# ---- counts on shaped posts -------------------------------------------------


async def test_counts_and_score_on_timeline_and_thread(hub):
    root = pid(await hub.feed_post("ada", "root"))
    reply = pid(await hub.feed_post("bob", "reply", parent_id=root))
    quiet = pid(await hub.feed_post("cy", "nobody reacts"))
    await hub.react("bob", root, "+1")
    await hub.react("cy", root, "+1")
    await hub.react("dee", root, "-1")
    await hub.react("ada", reply, "heart")

    page = await hub.feed_timeline()
    by_id = {post["id"]: post for post in page["posts"]}
    assert by_id[root]["reactions"] == {"+1": 2, "-1": 1} and by_id[root]["score"] == 1
    assert by_id[reply]["reactions"] == {"heart": 1} and by_id[reply]["score"] == 0
    assert by_id[quiet]["reactions"] == {} and by_id[quiet]["score"] == 0

    thread = await hub.feed_timeline(scope="thread", post_id=reply)
    top = thread["posts"][0]
    assert top["id"] == root and top["reactions"] == {"+1": 2, "-1": 1} and top["score"] == 1
    assert top["replies"][0]["reactions"] == {"heart": 1}

    single = (await hub.feed_get_post(root))["post"]
    assert single["score"] == 1

    # A quote carries the original's counts inline.
    quote = pid(await hub.feed_post("eve", "look", quote_of=root))
    shown = (await hub.feed_get_post(quote))["post"]
    assert shown["reactions"] == {} and shown["original"]["reactions"] == {"+1": 2, "-1": 1}

    counts = await hub.reaction_counts([root, quiet])
    assert counts == {
        root: {"reactions": {"+1": 2, "-1": 1}, "score": 1},
        quiet: {"reactions": {}, "score": 0},
    }


async def test_timeline_loads_reaction_counts_in_one_query(hub):
    ids = [pid(await hub.feed_post("ada", f"post {i}")) for i in range(5)]
    for post in ids:
        await hub.react("bob", post, "+1")
    seen: list[str] = []

    async def op(conn):
        await conn.set_trace_callback(seen.append)
        try:
            rows = await store.feed_timeline_rows(conn, before_id=None, limit=10)
            return await hub._shape_feed(conn, rows)
        finally:
            await conn.set_trace_callback(None)

    posts = await hub.db.read(op)
    assert [p["score"] for p in posts] == [1] * 5
    queries = [sql for sql in seen if "FROM reactions" in sql and not sql.lstrip().startswith("--")]
    assert len(queries) == 1


# ---- events -----------------------------------------------------------------


async def test_reaction_events_are_emitted_on_change_only(hub):
    post = pid(await hub.feed_post("ada", "events"))
    await hub.react("bob", post, "+1")
    await hub.react("bob", post, "+1")  # no change, no event
    await hub.react("bob", post, "-1")  # removes +1, adds -1
    await hub.unreact("bob", post, "-1")
    await hub.unreact("bob", post, "-1")  # no change, no event
    events = await reaction_events(hub)
    assert [(e["kind"], e["payload"]["kind"]) for e in events] == [
        ("reaction_added", "+1"),
        ("reaction_removed", "+1"),
        ("reaction_added", "-1"),
        ("reaction_removed", "-1"),
    ]
    first = events[0]
    assert first["channel_id"] is None and first["entity_id"] == post
    assert first["payload"] == {"post_id": post, "handle": "bob", "kind": "+1", "post_author": "ada"}
    filtered = await hub.wait_for_events(None, 0, kinds="reaction_added", timeout=0)
    assert [e["kind"] for e in filtered["events"]] == ["reaction_added", "reaction_added"]
    for event in events:
        assert event_actor(event) == "bob"
        assert "bob" in event_line(event)
        assert chat_payloads([event], "discord") and chat_payloads([event], "slack")


# ---- REST and MCP -----------------------------------------------------------


async def test_rest_reactions_require_auth(authed):
    client, _ = authed
    for method, path in (
        ("PUT", "/api/v1/feed/posts/1/reactions/+1"),
        ("DELETE", "/api/v1/feed/posts/1/reactions/+1"),
        ("GET", "/api/v1/feed/posts/1/reactions"),
    ):
        response = await client.request(method, path, json={"handle": "x"})
        assert response.status_code == 401, path


async def test_rest_react_unreact_list(running):
    client, _ = running
    created = await client.post("/api/v1/feed/posts", json={"author": "ada", "body": "rest"})
    post = created.json()["post"]["id"]

    up = await client.put(f"/api/v1/feed/posts/{post}/reactions/+1", json={"handle": "bob"})
    assert up.status_code == 201, up.text
    assert up.json()["reactions"] == {"+1": 1} and up.json()["score"] == 1
    again = await client.put(f"/api/v1/feed/posts/{post}/reactions/%2B1", params={"agent_name": "bob"})
    assert again.status_code == 200 and again.json()["idempotent"] is True
    down = await client.put(f"/api/v1/feed/posts/{post}/reactions/-1", json={"agent_name": "cy"})
    assert down.status_code == 201
    await client.put(f"/api/v1/feed/posts/{post}/reactions/heart", params={"handle": "cy"})

    listed = (await client.get(f"/api/v1/feed/posts/{post}/reactions")).json()
    assert listed["reactions"] == {"+1": 1, "-1": 1, "heart": 1} and listed["score"] == 0
    assert listed["handles"] == {"+1": ["bob"], "-1": ["cy"], "heart": ["cy"]}
    shaped = (await client.get(f"/api/v1/feed/posts/{post}")).json()["post"]
    assert shaped["reactions"] == {"+1": 1, "-1": 1, "heart": 1} and shaped["score"] == 0
    timeline = (await client.get("/api/v1/feed/timeline")).json()["posts"]
    assert timeline[0]["reactions"] == shaped["reactions"]

    removed = await client.request("DELETE", f"/api/v1/feed/posts/{post}/reactions/heart", json={"handle": "cy"})
    assert removed.status_code == 200 and removed.json()["mine"] == ["-1"]

    bad = await client.put(f"/api/v1/feed/posts/{post}/reactions/thumbsup", json={"handle": "bob"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation_error"
    assert bad.json()["error"]["details"]["allowed"] == list(feed.REACTION_KINDS)
    missing = await client.put("/api/v1/feed/posts/9999/reactions/+1", json={"handle": "bob"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await client.get("/api/v1/feed/posts/9999/reactions")).status_code == 404
    nobody = await client.put(f"/api/v1/feed/posts/{post}/reactions/+1")
    assert nobody.status_code == 400


async def test_mcp_reaction_tools(hub):
    mcp_app.set_hub(hub)
    try:
        async with Client(mcp_app.build_mcp()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"fleet_feed_react", "fleet_feed_unreact", "fleet_feed_list_reactions"} <= names
            post = pid(await hub.feed_post("ada", "mcp"))
            up = (
                await client.call_tool("fleet_feed_react", {"handle": "bob", "post_id": post, "kind": "+1"})
            ).structured_content
            assert up["reactions"] == {"+1": 1} and up["score"] == 1
            await client.call_tool("fleet_feed_react", {"handle": "cy", "post_id": post, "kind": "rocket"})
            listed = (
                await client.call_tool("fleet_feed_list_reactions", {"post_id": post})
            ).structured_content
            assert listed["handles"] == {"+1": ["bob"], "rocket": ["cy"]}
            gone = (
                await client.call_tool("fleet_feed_unreact", {"handle": "bob", "post_id": post, "kind": "+1"})
            ).structured_content
            assert gone["reactions"] == {"rocket": 1} and gone["score"] == 0
            bad = (
                await client.call_tool("fleet_feed_react", {"handle": "bob", "post_id": post, "kind": "nope"})
            ).structured_content
            assert bad["status"] == "error" and bad["error"] == "validation_error"
            missing = (
                await client.call_tool("fleet_feed_react", {"handle": "bob", "post_id": 9_999, "kind": "+1"})
            ).structured_content
            assert missing["status"] == "error" and missing["error"] == "not_found"
    finally:
        mcp_app.set_hub(None)


# ---- schema -----------------------------------------------------------------


async def test_v10_database_migrates_to_the_reactions_table(tmp_path):
    path = tmp_path / "v10.sqlite"
    db = Database(str(path))
    await db.open()
    try:
        old = pid(await Hub(db, WaitHub()).feed_post("ada", "from v10 #kept"))
    finally:
        await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE reactions")
        conn.execute("DELETE FROM schema_migrations WHERE version > 10")

    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        kept = (await hub.feed_get_post(old))["post"]
        assert kept["body"] == "from v10 #kept" and kept["reactions"] == {} and kept["score"] == 0
        assert (await hub.react("bob", old, "+1"))["score"] == 1
    finally:
        await db.close()
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        foreign = conn.execute("PRAGMA foreign_key_list(reactions)").fetchall()
    assert "reactions" in tables
    assert versions == list(range(1, SCHEMA_VERSION + 1)) and versions[-1] == 12
    assert any(row[2] == "feed_posts" and row[6] == "CASCADE" for row in foreign)


async def test_hard_deleted_post_cascades_its_reactions(hub):
    post = pid(await hub.feed_post("ada", "hard delete"))
    await hub.react("bob", post, "+1")

    async def purge(conn):
        await conn.execute("DELETE FROM feed_hashtags WHERE post_id = ?", (post,))
        await conn.execute("DELETE FROM feed_posts WHERE id = ?", (post,))

    await hub._txn(purge)
    assert await stored_rows(hub, post) == []
