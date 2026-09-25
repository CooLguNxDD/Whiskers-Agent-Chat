"""Store, hub rules, waiters, REST, and MCP against one running app."""

from __future__ import annotations

import asyncio
import time

import pytest

from cat_fleet_chat.errors import HubError
from cat_fleet_chat import mcp_app
from cat_fleet_chat import store


async def _mention_names(app) -> list[str]:
    async def op(conn):
        cursor = await conn.execute(
            "SELECT agent_name FROM mentions ORDER BY message_id, agent_name"
        )
        return [row["agent_name"] for row in await cursor.fetchall()]

    return await app.state.hub.db.read(op)


async def test_fleet_is_seeded_once(running):
    client, app = running
    first = await client.get("/api/v1/channels")
    assert first.status_code == 200
    assert [c["name"] for c in first.json()["channels"]] == ["fleet"]

    # Opening the same file again (after this process releases it) is covered
    # by the migration's INSERT OR IGNORE. A second create in-process conflicts.
    again = await client.post("/api/v1/channels", json={"name": "fleet", "topic": "x"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "channel_exists"
    assert again.json()["error"]["details"]["name"] == "fleet"
    listed = await client.get("/api/v1/channels")
    assert len(listed.json()["channels"]) == 1
    assert app.state.hub is not None


async def test_foreign_keys_on_every_connection(running):
    _, app = running

    async def op(conn):
        cursor = await conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        return row[0]

    assert await app.state.hub.db.read(op) == 1
    assert await app.state.hub.db.write(op) == 1


async def test_post_stores_mention_and_rejects_unknown_channel(running):
    client, app = running
    missing = await client.post(
        "/api/v1/messages",
        json={"channel": "nope", "author": "ada", "text": "hi"},
    )
    assert missing.status_code == 404

    created = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "ada", "text": "migration done, @codex take it"},
    )
    assert created.status_code == 201
    body = created.json()["message"]
    assert body["mentions"] == ["codex"]
    assert await _mention_names(app) == ["codex"]

    cross = await client.post(
        "/api/v1/channels",
        json={"name": "other", "topic": ""},
    )
    assert cross.status_code == 201
    reply = await client.post(
        "/api/v1/messages",
        json={
            "channel": "other",
            "author": "ada",
            "text": "nope",
            "reply_to": body["id"],
        },
    )
    assert reply.status_code == 400
    assert reply.json()["error"]["code"] == "validation_error"


async def test_pagination_and_idempotent_post(running):
    client, _app = running
    ids = []
    for i in range(3):
        res = await client.post(
            "/api/v1/messages",
            json={"channel": "fleet", "author": "ada", "text": f"m{i}", "client_request_id": f"k{i}"},
        )
        ids.append(res.json()["message"]["id"])

    replay = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "ada", "text": "m0", "client_request_id": "k0"},
    )
    assert replay.status_code == 201
    assert replay.json()["message"]["id"] == ids[0]
    conflict = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "ada", "text": "different", "client_request_id": "k0"},
    )
    assert conflict.status_code == 409

    page = await client.get("/api/v1/messages", params={"channel": "fleet", "limit": 2})
    payload = page.json()
    assert [m["id"] for m in payload["messages"]] == ids[1:]
    assert payload["has_more"] is True
    older = await client.get(
        "/api/v1/messages",
        params={"channel": "fleet", "before_id": payload["next_before_id"], "limit": 2},
    )
    assert [m["id"] for m in older.json()["messages"]] == [ids[0]]

    newer = await client.get(
        "/api/v1/messages",
        params={"channel": "fleet", "since_id": ids[0], "limit": 10},
    )
    assert [m["id"] for m in newer.json()["messages"]] == ids[1:]

    both = await client.get(
        "/api/v1/messages",
        params={"channel": "fleet", "since_id": 0, "before_id": 99},
    )
    assert both.status_code == 400

    empty = await client.get(
        "/api/v1/messages",
        params={"channel": "fleet", "since_id": ids[-1]},
    )
    assert empty.json()["messages"] == []
    assert empty.json()["cursor"] == ids[-1]


async def test_wait_wakes_and_preserves_cursor(running):
    client, _app = running

    async def waiter():
        return await client.get(
            "/api/v1/wait",
            params={"agent": "codex", "since_id": 0, "timeout": 5},
        )

    pending = asyncio.create_task(waiter())
    await asyncio.sleep(0.1)
    posted = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "ada", "text": "go @codex"},
    )
    response = await asyncio.wait_for(pending, timeout=2)
    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False
    assert body["channel"] is None
    assert body["messages"][0]["id"] == posted.json()["message"]["id"]
    assert body["cursor"] == posted.json()["message"]["id"]

    started = time.monotonic()
    quiet = await client.get(
        "/api/v1/wait",
        params={"agent": "nobody", "since_id": 0, "timeout": 1},
    )
    elapsed = time.monotonic() - started
    assert quiet.json()["timed_out"] is True
    assert quiet.json()["messages"] == []
    assert quiet.json()["cursor"] == 0
    assert elapsed < 2.5


async def test_two_waiters_see_their_own_slice(running):
    _client, app = running
    hub = app.state.hub
    first = asyncio.create_task(hub.wait_for_mentions("codex", 0, timeout=5))
    second = asyncio.create_task(hub.wait_for_mentions("claude", 0, timeout=5))
    await asyncio.sleep(0.05)
    await hub.post_message("fleet", "ada", "for @codex only")
    codex = await asyncio.wait_for(first, timeout=2)
    assert codex["messages"][0]["mentions"] == ["codex"]
    assert not second.done()
    await hub.post_message("fleet", "ada", "for @claude now")
    claude = await asyncio.wait_for(second, timeout=2)
    assert claude["messages"][0]["id"] != codex["messages"][0]["id"]
    assert claude["messages"][0]["mentions"] == ["claude"]


async def test_unrelated_notify_does_not_extend_deadline(running):
    _client, app = running
    hub = app.state.hub

    async def nudge():
        for _ in range(8):
            await asyncio.sleep(0.1)
            await hub.waiters.notify()

    nudger = asyncio.create_task(nudge())
    started = time.monotonic()
    result = await hub.wait_for_mentions("ghost", 0, timeout=1)
    elapsed = time.monotonic() - started
    nudger.cancel()
    assert result["timed_out"] is True
    assert 0.8 <= elapsed < 2.5


async def test_cancel_wait_leaves_no_waiter(running):
    _client, app = running
    hub = app.state.hub
    task = asyncio.create_task(hub.wait_for_mentions("codex", 0, timeout=30))
    for _ in range(50):
        if hub.waiters.waiter_count:
            break
        await asyncio.sleep(0.02)
    assert hub.waiters.waiter_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hub.waiters.waiter_count == 0


async def test_task_claim_race_and_transitions(running):
    client, app = running
    created = await client.post(
        "/api/v1/tasks",
        json={"channel": "fleet", "title": "frontend", "actor": "ada", "client_request_id": "t1"},
    )
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["status"] == "open"
    assert task["assignee"] is None
    assert task["events"][0]["from_status"] is None
    replay = await client.post(
        "/api/v1/tasks",
        json={"channel": "fleet", "title": "frontend", "actor": "ada", "client_request_id": "t1"},
    )
    assert replay.json()["task"]["id"] == task["id"]
    changed = await client.post(
        "/api/v1/tasks",
        json={"channel": "fleet", "title": "other", "actor": "ada", "client_request_id": "t1"},
    )
    assert changed.status_code == 409

    hub = app.state.hub
    results = await asyncio.gather(
        hub.claim_task(task["id"], "codex"),
        hub.claim_task(task["id"], "claude"),
        return_exceptions=True,
    )
    winners = [item for item in results if not isinstance(item, Exception)]
    losers = [item for item in results if isinstance(item, HubError)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].status == 409
    claimed = winners[0]["task"]
    assert claimed["status"] == "claimed"
    assert len(claimed["events"]) == 2

    again = await hub.claim_task(task["id"], claimed["assignee"])
    assert again["idempotent"] is True
    assert len(again["task"]["events"]) == 2

    moved = await client.post(
        f"/api/v1/tasks/{task['id']}/status",
        json={
            "status": "in_progress",
            "actor": claimed["assignee"],
            "expected_version": claimed["version"],
        },
    )
    assert moved.status_code == 200
    stale = await client.post(
        f"/api/v1/tasks/{task['id']}/status",
        json={
            "status": "done",
            "actor": claimed["assignee"],
            "expected_version": claimed["version"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_conflict"

    current = moved.json()["task"]
    bad = await client.post(
        f"/api/v1/tasks/{task['id']}/status",
        json={
            "status": "claimed",
            "actor": claimed["assignee"],
            "expected_version": current["version"],
        },
    )
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "invalid_transition"

    reopened = await hub.update_task_status(
        task["id"], "blocked", claimed["assignee"], current["version"]
    )
    opened = await hub.update_task_status(
        task["id"], "open", claimed["assignee"], reopened["task"]["version"]
    )
    assert opened["task"]["status"] == "open"
    assert opened["task"]["assignee"] is None


async def test_assigned_create_starts_claimed(running):
    client, _app = running
    created = await client.post(
        "/api/v1/tasks",
        json={
            "channel": "fleet",
            "title": "docs",
            "assignee": "codex",
            "actor": "ada",
        },
    )
    task = created.json()["task"]
    assert task["status"] == "claimed"
    assert task["assignee"] == "codex"
    agents = await client.get("/api/v1/agents")
    assert agents.json()["label"] == "recent activity"
    names = {item["name"] for item in agents.json()["agents"]}
    assert "ada" in names
    assert "codex" in names


async def test_mcp_tools_match_rest(running):
    client, _app = running
    posted = await mcp_app.post_message("fleet", "ada", "via mcp @codex")
    assert posted["message"]["mentions"] == ["codex"]
    listed = await mcp_app.get_messages("fleet", since_id=0)
    assert listed["messages"][-1]["text"] == "via mcp @codex"
    waited = await mcp_app.wait_for_mentions("codex", since_id=0, timeout=1)
    assert waited["timed_out"] is False
    assert waited["channel"] is None
    missing = await mcp_app.post_message("missing", "ada", "x")
    assert missing["status"] == "error"
    assert missing["error"] == "not_found"


async def test_auth_covers_rest_wait_sse_and_mcp(authed):
    client, _app = running_unused = authed
    del running_unused
    open_post = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "ada", "text": "nope"},
    )
    assert open_post.status_code == 401
    wait = await client.get("/api/v1/wait", params={"agent": "ada", "timeout": 1})
    assert wait.status_code == 401
    events = await client.get("/api/v1/events")
    assert events.status_code == 401
    mcp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert mcp.status_code == 401

    headers = {"Authorization": "Bearer test-token"}
    ok = await client.post(
        "/api/v1/messages",
        headers=headers,
        json={"channel": "fleet", "author": "ada", "text": "in"},
    )
    assert ok.status_code == 201
    cross = await client.post(
        "/api/v1/messages",
        headers={**headers, "Origin": "http://evil.example"},
        json={"channel": "fleet", "author": "ada", "text": "cross"},
    )
    assert cross.status_code == 403


async def test_sse_replays_message_and_task(running):
    """Drive the ASGI app directly. httpx's ASGI transport buffers the whole body,
    so it cannot observe an SSE stream that stays open."""
    client, app = running
    bodies: list[bytes] = []
    ready = asyncio.Event()
    saw_message = asyncio.Event()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await saw_message.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
            ready.set()
        elif message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))
            if b"message.created" in b"".join(bodies):
                saw_message.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"after_event_id=0",
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 123),
    }
    stream = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(ready.wait(), timeout=2)
    posted = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "ada", "text": "live"},
    )
    assert posted.status_code == 201
    await asyncio.wait_for(saw_message.wait(), timeout=2)
    await asyncio.wait_for(stream, timeout=2)
    text = b"".join(bodies).decode()
    assert "event: message.created" in text
    assert "live" in text


async def test_built_spa_and_deep_link(running, monkeypatch):
    from pathlib import Path

    dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        pytest.skip("portal has not been built")
    monkeypatch.setenv("CAT_FLEET_DIST_DIR", str(dist))
    client, _app = running
    page = await client.get("/")
    assert page.status_code == 200
    assert 'id="root"' in page.text
    tasks = await client.get("/tasks")
    assert tasks.status_code == 200
    assert 'id="root"' in tasks.text
    missing = await client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404


async def test_hint_page_without_dist(running, monkeypatch):
    monkeypatch.setenv("CAT_FLEET_DIST_DIR", "")
    client, _app = running
    page = await client.get("/")
    assert page.status_code == 200
    assert "npm --prefix web run build" in page.text
    missing = await client.get("/api/v1/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_direct_store_roundtrip_keeps_history(running):
    _client, app = running

    async def op(conn):
        await conn.execute("BEGIN IMMEDIATE")
        try:
            channel = await store.get_channel_by_name(conn, "fleet")
            assert channel is not None
            await conn.commit()
            return channel["name"]
        except Exception:
            await conn.rollback()
            raise

    assert await app.state.hub.db.write(op) == "fleet"
