"""Regression coverage for reaction review findings (bounds, tombstones and transports)."""

from __future__ import annotations

import sqlite3

import pytest

from cat_fleet_chat import feed, store
from cat_fleet_chat.db import Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import event_line


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "reaction-limits.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


async def test_list_caps_handles_per_kind_in_sql_but_keeps_exact_totals(hub):
    post = (await hub.feed_post("ada", "popular"))["post"]["id"]
    count = 103

    async def seed(conn):
        await conn.executemany(
            "INSERT INTO reactions(post_id, handle, kind, created_at) VALUES (?, ?, ?, ?)",
            [(post, f"agent{i}", kind, "t") for i in range(count) for kind in feed.REACTION_KINDS],
        )
        # Add another downvote so score must use full totals, not the sample.
        await store.insert_reaction(conn, post, "extra", "-1", "t")

    await hub._txn(seed)
    listed = await hub.list_reactions(post)
    assert listed["reactions"] == {kind: count + (kind == "-1") for kind in feed.REACTION_KINDS}
    assert listed["score"] == -1
    assert listed["handles"] == {kind: [f"agent{i}" for i in range(100)] for kind in feed.REACTION_KINDS}
    # The store itself must not materialize an unbounded result for the hub.
    rows = await hub.db.read(lambda conn: store.reaction_rows(conn, post))
    assert len(rows) == 100 * len(feed.REACTION_KINDS)
    assert all(row["total"] == count + (row["kind"] == "-1") for row in rows)
    assert await hub.list_reactions(post) == listed  # stable oldest-first sampling
    repost = (await hub.feed_repost("bob", post))["post"]["id"]
    assert await hub.list_reactions(repost) == listed


async def test_deleted_post_tombstones_hide_reactions_and_listing_is_not_found(hub):
    post = (await hub.feed_post("ada", "gone"))["post"]["id"]
    await hub.feed_post("bob", "reply remains", parent_id=post)
    repost = (await hub.feed_repost("cy", post))["post"]["id"]
    await hub.react("bob", post, "+1")
    await hub.react("cy", post, "heart")
    await hub.feed_delete("ada", post)
    single = (await hub.feed_get_post(post))["post"]
    thread = (await hub.feed_timeline(scope="thread", post_id=post))["posts"][0]
    for tombstone in (single, thread):
        assert tombstone["deleted"] is True
        assert tombstone["reactions"] == {}
        assert tombstone["score"] == 0
    for target in (post, repost):
        with pytest.raises(HubError) as caught:
            await hub.list_reactions(target)
        assert caught.value.code == "not_found" and caught.value.status == 404


async def test_listing_deleted_repost_is_not_found_even_if_original_is_live(hub):
    post = (await hub.feed_post("ada", "live"))["post"]["id"]
    repost = (await hub.feed_repost("bob", post))["post"]["id"]
    await hub.react("cy", post, "+1")
    await hub.feed_delete("bob", repost)
    with pytest.raises(HubError) as caught:
        await hub.list_reactions(repost)
    assert caught.value.status == 404
    assert (await hub.list_reactions(post))["handles"] == {"+1": ["cy"]}


async def test_unheld_opposite_vote_does_not_emit_removed_event(hub):
    post = (await hub.feed_post("ada", "vote"))["post"]["id"]
    await hub.react("cy", post, "-1")  # somebody else's vote must stay
    await hub.react("bob", post, "heart")
    first = await hub.react("bob", post, "+1")
    again = await hub.react("bob", post, "+1")
    assert first["replaced"] == again["replaced"] == []
    assert first["reactions"] == {"+1": 1, "-1": 1, "heart": 1}
    assert again["idempotent"] is True
    events = await hub.events_after(0, channel=None, limit=100)
    assert not any(event["kind"] == "reaction_removed" for event in events)


@pytest.mark.parametrize("event_kind", ["reaction_added", "reaction_removed"])
@pytest.mark.parametrize("kind,icon", [
    ("+1", "👍"), ("-1", "👎"), ("heart", "❤️"), ("laugh", "😄"),
    ("eyes", "👀"), ("tada", "🎉"), ("rocket", "🚀"), ("thinking", "🤔"),
    ("future-kind", "💬"),
])
def test_reaction_event_icon_matches_kind(event_kind, kind, icon):
    line = event_line({"kind": event_kind, "payload": {
        "kind": kind, "handle": "bob", "post_author": "ada", "post_id": 1,
    }})
    assert line.startswith(icon + " ")
    assert f" {kind} on " in line


@pytest.mark.parametrize("content", [b"", b"{invalid-json"])
@pytest.mark.parametrize("caller_key", ["handle", "agent_name"])
async def test_rest_delete_reaction_ignores_json_body(running, content, caller_key):
    client, _ = running
    post = (await client.post("/api/v1/feed/posts", json={"author": "ada", "body": "rest"})).json()["post"]["id"]
    path = f"/api/v1/feed/posts/{post}/reactions/heart"
    assert (await client.put(path, json={"handle": "bob"})).status_code == 201
    response = await client.request("DELETE", path, params={caller_key: "bob"},
                                    content=content, headers={"content-type": "application/json"})
    assert response.status_code == 200, response.text
    assert response.json()["reactions"] == {} and response.json()["idempotent"] is False
    assert (await client.delete(path, params={caller_key: "bob"})).json()["idempotent"] is True


async def test_reaction_handle_index_is_added_to_existing_database_idempotently(tmp_path):
    path = tmp_path / "old.sqlite"
    db = Database(str(path))
    await db.open()
    hub = Hub(db, WaitHub())
    post = (await hub.feed_post("ada", "keep data"))["post"]["id"]
    await hub.react("bob", post, "+1")
    await db.close()
    with sqlite3.connect(path) as conn:
        # Simulate an existing v12 database before the index was added.
        conn.execute("DROP INDEX IF EXISTS idx_reactions_handle")
    for _ in range(2):
        db = Database(str(path))
        await db.open()
        await db.close()
    with sqlite3.connect(path) as conn:
        columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_reactions_handle)")]
        assert columns == ["handle"]
        assert conn.execute("SELECT handle, kind FROM reactions").fetchall() == [("bob", "+1")]
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM reactions WHERE handle = 'bob'").fetchall()
        assert any("idx_reactions_handle" in row[3] for row in plan)
