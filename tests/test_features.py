"""Channel archive, attachment metadata, agent notifications, the listener, and the v2 migration."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from cat_fleet_chat import listen, mcp_app
from cat_fleet_chat.db import Database
from cat_fleet_chat.hub import agent_relevant

ATTACHMENT = {
    "filename": "report.md",
    "content_type": "text/markdown",
    "size_bytes": 42,
    "storage": "minio",
    "bucket": "cat-fleet-attachments",
    "object_key": "fleet/work/abc/report.md",
    "sha256": "a" * 64,
}


async def _channel(client, name: str) -> None:
    created = await client.post("/api/v1/channels", json={"name": name, "topic": ""})
    assert created.status_code == 201


async def _post(client, channel: str, author: str, text: str, **extra):
    return await client.post(
        "/api/v1/messages",
        json={"channel": channel, "author": author, "text": text, **extra},
    )


# --- archive ---------------------------------------------------------------


async def test_fleet_cannot_be_archived(running):
    client, _ = running
    refused = await client.post("/api/v1/channels/fleet/archive", json={"actor": "ada"})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"


async def test_archive_refuses_open_tasks_then_force_cancels(running):
    client, _ = running
    await _channel(client, "work")
    task = (
        await client.post(
            "/api/v1/tasks", json={"channel": "work", "title": "ship", "actor": "ada"}
        )
    ).json()["task"]

    refused = await client.post("/api/v1/channels/work/archive", json={"actor": "ada"})
    assert refused.status_code == 409
    body = refused.json()["error"]
    assert body["code"] == "channel_has_open_tasks"
    assert body["details"]["task_ids"] == [task["id"]]

    forced = await client.post(
        "/api/v1/channels/work/archive",
        json={"actor": "ada", "note": "shipped", "force": True},
    )
    assert forced.status_code == 200
    result = forced.json()
    assert result["cancelled_tasks"] == [task["id"]]
    assert result["channel"]["archived_by"] == "ada"
    assert result["channel"]["archived_at"]

    tasks = (await client.get("/api/v1/tasks", params={"channel": "work"})).json()["tasks"]
    assert tasks[0]["status"] == "cancelled"
    assert tasks[0]["events"][-1]["note"] == "channel archived: shipped"

    again = await client.post("/api/v1/channels/work/archive", json={"actor": "ada"})
    assert again.json()["idempotent"] is True


async def test_archived_channel_is_read_only_and_hidden(running):
    client, _ = running
    await _channel(client, "done")
    first = await _post(client, "done", "ada", "last word")
    assert first.status_code == 201
    done_task = (
        await client.post(
            "/api/v1/tasks", json={"channel": "done", "title": "t", "assignee": "ada"}
        )
    ).json()["task"]
    await client.post(
        f"/api/v1/tasks/{done_task['id']}/status",
        json={"status": "in_progress", "actor": "ada", "expected_version": 1},
    )
    await client.post(
        f"/api/v1/tasks/{done_task['id']}/status",
        json={"status": "done", "actor": "ada", "expected_version": 2},
    )

    archived = await client.post("/api/v1/channels/done/archive", json={"actor": "ada"})
    assert archived.status_code == 200
    assert archived.json()["cancelled_tasks"] == []

    blocked = await _post(client, "done", "ada", "one more")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "channel_archived"
    no_task = await client.post("/api/v1/tasks", json={"channel": "done", "title": "x", "actor": "ada"})
    assert no_task.json()["error"]["code"] == "channel_archived"

    history = await client.get("/api/v1/messages", params={"channel": "done"})
    assert [m["text"] for m in history.json()["messages"]] == ["last word"]

    visible = [c["name"] for c in (await client.get("/api/v1/channels")).json()["channels"]]
    assert "done" not in visible
    everything = (
        await client.get("/api/v1/channels", params={"include_archived": "1"})
    ).json()["channels"]
    assert {c["name"] for c in everything} == {"fleet", "done"}

    reopened = await client.post("/api/v1/channels/done/unarchive", json={"actor": "ada"})
    assert reopened.json()["channel"]["archived_at"] is None
    assert (await _post(client, "done", "ada", "back")).status_code == 201

    kinds = [e["kind"] for e in (await client.get(
        "/api/v1/notifications", params={"timeout": 0, "kinds": "channel.archived,channel.unarchived"}
    )).json()["events"]]
    assert kinds == ["channel.archived", "channel.unarchived"]


# --- attachments -----------------------------------------------------------


async def test_attachment_roundtrip(running):
    client, _ = running
    posted = await _post(client, "fleet", "ada", "see file", attachments=[ATTACHMENT])
    assert posted.status_code == 201
    message = posted.json()["message"]
    assert len(message["attachments"]) == 1
    stored = message["attachments"][0]
    assert stored["bucket"] == "cat-fleet-attachments"
    assert stored["message_id"] == message["id"]

    page = await client.get("/api/v1/messages", params={"channel": "fleet"})
    assert page.json()["messages"][-1]["attachments"][0]["id"] == stored["id"]

    fetched = await client.get(f"/api/v1/attachments/{stored['id']}")
    assert fetched.status_code == 200
    body = fetched.json()["attachment"]
    assert body["channel"] == "fleet"
    assert body["uploaded_by"] == "ada"
    assert body["object_key"] == ATTACHMENT["object_key"]

    missing = await client.get("/api/v1/attachments/9999")
    assert missing.status_code == 404

    plain = await _post(client, "fleet", "ada", "no file")
    assert plain.json()["message"]["attachments"] == []


@pytest.mark.parametrize(
    "patch",
    [
        {"filename": "../etc/passwd"},
        {"object_key": "a/../b"},
        {"object_key": "/abs"},
        {"bucket": "Bad_Bucket"},
        {"storage": "s3"},
        {"size_bytes": -1},
        {"content_type": "not a type"},
        {"sha256": "xyz"},
    ],
)
async def test_bad_attachment_is_rejected(running, patch):
    client, _ = running
    bad = await _post(client, "fleet", "ada", "x", attachments=[{**ATTACHMENT, **patch}])
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "validation_error"


async def test_attachment_cap_and_idempotency(running):
    client, _ = running
    too_many = await _post(client, "fleet", "ada", "x", attachments=[ATTACHMENT] * 11)
    assert too_many.status_code == 400

    first = await _post(
        client, "fleet", "ada", "x", attachments=[ATTACHMENT], client_request_id="k1"
    )
    replay = await _post(
        client, "fleet", "ada", "x", attachments=[ATTACHMENT], client_request_id="k1"
    )
    assert replay.json()["message"]["id"] == first.json()["message"]["id"]
    changed = await _post(
        client,
        "fleet",
        "ada",
        "x",
        attachments=[{**ATTACHMENT, "filename": "other.md"}],
        client_request_id="k1",
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_conflict"


# --- notifications ---------------------------------------------------------


def test_agent_relevance_rules():
    mention = {"kind": "message.created", "payload": {"author": "ada", "mentions": ["codex"]}}
    assert agent_relevant(mention, "codex")
    assert not agent_relevant(mention, "grok")
    own = {"kind": "message.created", "payload": {"author": "codex", "mentions": ["codex"]}}
    assert not agent_relevant(own, "codex")
    assigned = {
        "kind": "task.updated",
        "payload": {"assignee": "codex", "events": [{"actor": "ada"}]},
    }
    assert agent_relevant(assigned, "codex")
    self_update = {
        "kind": "task.updated",
        "payload": {"assignee": "codex", "events": [{"actor": "codex"}]},
    }
    assert not agent_relevant(self_update, "codex")
    assert agent_relevant({"kind": "channel.archived", "payload": {}}, "codex")
    assert agent_relevant(own, None)


async def test_notifications_filter_and_advance_cursor(running):
    client, _ = running
    await _post(client, "fleet", "ada", "noise")
    await _post(client, "fleet", "ada", "hey @codex")
    await _post(client, "fleet", "codex", "talking to myself @codex")

    page = (
        await client.get(
            "/api/v1/notifications", params={"agent": "codex", "timeout": 0}
        )
    ).json()
    assert [e["payload"]["text"] for e in page["events"]] == ["hey @codex"]
    assert page["timed_out"] is False
    # The cursor moved past the trailing irrelevant event too.
    latest = (await client.get("/api/v1/events/cursor")).json()["event_cursor"]
    assert page["cursor"] == latest

    empty = (
        await client.get(
            "/api/v1/notifications",
            params={"agent": "codex", "timeout": 0, "after_event_id": page["cursor"]},
        )
    ).json()
    assert empty["events"] == [] and empty["timed_out"] is False
    assert empty["cursor"] == page["cursor"]


async def test_notifications_limit_keeps_cursor_on_last_returned(running):
    client, _ = running
    for index in range(3):
        await _post(client, "fleet", "ada", f"@codex {index}")
    page = (
        await client.get(
            "/api/v1/notifications", params={"agent": "codex", "timeout": 0, "limit": 2}
        )
    ).json()
    assert len(page["events"]) == 2
    assert page["has_more"] is True
    assert page["cursor"] == page["events"][-1]["id"]


async def test_notifications_wait_wakes_on_match(running):
    client, _ = running

    async def waiter():
        return await client.get(
            "/api/v1/notifications", params={"agent": "grok", "timeout": 10}
        )

    pending = asyncio.create_task(waiter())
    await asyncio.sleep(0.1)
    await _post(client, "fleet", "ada", "unrelated")
    await asyncio.sleep(0.05)
    assert not pending.done()
    await _post(client, "fleet", "ada", "@grok ping")
    result = (await asyncio.wait_for(pending, 5)).json()
    assert [e["payload"]["text"] for e in result["events"]] == ["@grok ping"]


async def test_notifications_bad_kind_and_unknown_channel(running):
    client, _ = running
    bad = await client.get("/api/v1/notifications", params={"kinds": "nope", "timeout": 0})
    assert bad.status_code == 400
    missing = await client.get("/api/v1/notifications", params={"channel": "nope", "timeout": 0})
    assert missing.status_code == 404


async def test_sse_agent_filter(running):
    """Raw ASGI, as in test_hub: httpx's ASGI transport buffers an open stream."""
    client, app = running
    await _post(client, "fleet", "ada", "noise")
    await _post(client, "fleet", "ada", "@codex look")
    bodies: list[bytes] = []
    got_frame = asyncio.Event()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await got_frame.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))
            if b"data: " in b"".join(bodies):
                got_frame.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"after_event_id=0&agent=codex&kinds=message.created",
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 123),
    }
    stream = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(got_frame.wait(), timeout=2)
    await asyncio.wait_for(stream, timeout=20)
    frames = [
        json.loads(line[6:])
        for line in b"".join(bodies).decode().splitlines()
        if line.startswith("data: ")
    ]
    assert [f["payload"]["text"] for f in frames] == ["@codex look"]


async def test_new_routes_require_token(authed):
    client, _ = authed
    for path in (
        "/api/v1/notifications?timeout=0",
        "/api/v1/events/cursor",
        "/api/v1/attachments/1",
    ):
        assert (await client.get(path)).status_code == 401
    assert (await client.post("/api/v1/channels/x/archive", json={})).status_code == 401


async def test_mcp_tools_match_rest(running):
    client, _ = running
    await _channel(client, "mcp")
    archived = await mcp_app.archive_channel("mcp", "ada")
    assert archived["channel"]["archived_by"] == "ada"
    listed = await mcp_app.list_channels(include_archived=True)
    assert "mcp" in {c["name"] for c in listed["channels"]}
    refused = await mcp_app.post_message("mcp", "ada", "x")
    assert refused["status"] == "error" and refused["error"] == "channel_archived"
    events = await mcp_app.wait_for_events(kinds=["channel.archived"], timeout=0)
    assert [e["kind"] for e in events["events"]] == ["channel.archived"]


# --- listener --------------------------------------------------------------


async def test_listener_once_delivers_and_saves_cursor(running, tmp_path):
    client, _ = running
    await _post(client, "fleet", "ada", "@codex before listener")
    state = tmp_path / "cursor"
    args = listen.build_parser().parse_args(
        [
            "--url",
            "http://testserver",
            "--agent",
            "codex",
            "--once",
            "--poll-timeout",
            "5",
            "--state-file",
            str(state),
        ]
    )
    seen: list[dict] = []
    task = asyncio.create_task(listen.listen(args, client, emit=seen.append))
    await asyncio.sleep(0.1)
    await _post(client, "fleet", "ada", "@codex after listener")
    cursor = await asyncio.wait_for(task, 5)
    # --after now skipped the earlier mention.
    assert [e["payload"]["text"] for e in seen] == ["@codex after listener"]
    assert int(state.read_text()) == cursor

    # A saved cursor resumes without replaying.
    await _post(client, "fleet", "ada", "@codex third")
    seen.clear()
    await asyncio.wait_for(listen.listen(args, client, emit=seen.append), 5)
    assert [e["payload"]["text"] for e in seen] == ["@codex third"]


async def test_listener_stops_on_client_error(running):
    client, _ = running
    args = listen.build_parser().parse_args(
        ["--url", "http://testserver", "--agent", "bad name", "--after", "0"]
    )
    with pytest.raises(SystemExit):
        await listen.listen(args, client, emit=lambda _: None)

    # A bad --after must exit, not be retried as a transient ValueError.
    bad_after = listen.build_parser().parse_args(["--url", "http://testserver", "--after", "abc"])
    with pytest.raises(SystemExit):
        await asyncio.wait_for(listen.listen(bad_after, client, emit=lambda _: None), 5)


# --- migration -------------------------------------------------------------

V1_DDL = """
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE channels (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES channels(id),
    author TEXT NOT NULL, text TEXT NOT NULL, reply_to INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL
);
INSERT INTO schema_migrations VALUES (1, '2026-01-01');
INSERT INTO channels (name, topic, created_at) VALUES ('fleet', 'Fleet-wide chat', '2026-01-01');
INSERT INTO messages (channel_id, author, text, created_at) VALUES (1, 'ada', 'old', '2026-01-01');
"""


async def test_v1_database_upgrades_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_DDL)

    for _ in range(2):  # the second open must be a no-op
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        fleets = conn.execute("SELECT COUNT(*) FROM channels WHERE name='fleet'").fetchone()[0]
        old = conn.execute("SELECT text FROM messages").fetchall()
    assert {"archived_at", "archived_by", "state", "state_note"} <= columns
    assert {"attachments", "tasks", "events", "idempotency", "mentions"} <= tables
    assert versions == [1, 2, 3, 4]
    assert fleets == 1
    assert old == [("old",)]
