"""Channel lifecycle state: labels, the archived state, events, filters, MCP, and the v3 backfill."""

from __future__ import annotations

import sqlite3

from cat_fleet_chat import mcp_app
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.hub import agent_relevant


async def _channel(client, name: str) -> dict:
    created = await client.post("/api/v1/channels", json={"name": name, "topic": ""})
    assert created.status_code == 201
    return created.json()["channel"]


async def _set(client, name: str, state: str, **extra):
    return await client.post(
        f"/api/v1/channels/{name}/state", json={"state": state, "actor": "ada", **extra}
    )


async def _events(client, kinds: str) -> list[dict]:
    page = await client.get("/api/v1/notifications", params={"timeout": 0, "kinds": kinds})
    return page.json()["events"]


async def test_new_channel_starts_active(running):
    client, _ = running
    channel = await _channel(client, "work")
    assert channel["state"] == "active"
    assert channel["state_note"] == ""
    fleet = (await client.get("/api/v1/channels")).json()["channels"][0]
    assert fleet["state"] == "active"


async def test_label_states_change_and_emit(running):
    client, _ = running
    await _channel(client, "work")
    blocked = await _set(client, "work", "blocked", note="waiting on the CA cert")
    assert blocked.status_code == 200
    body = blocked.json()
    assert body["previous_state"] == "active"
    assert body["channel"]["state"] == "blocked"
    assert body["channel"]["state_note"] == "waiting on the CA cert"
    assert body["channel"]["state_updated_by"] == "ada"
    assert body["channel"]["archived_at"] is None

    # Labels do not restrict writes.
    posted = await client.post(
        "/api/v1/messages", json={"channel": "work", "author": "ada", "text": "still here"}
    )
    assert posted.status_code == 201

    again = await _set(client, "work", "blocked")
    assert again.json()["idempotent"] is True

    await _set(client, "work", "review")
    changes = await _events(client, "channel.state_changed")
    assert [(e["payload"]["previous_state"], e["payload"]["state"]) for e in changes] == [
        ("active", "blocked"),
        ("blocked", "review"),
    ]
    # Channel lifecycle events are broadcast to every agent.
    assert agent_relevant(changes[0], "codex")


async def test_bad_state_is_rejected(running):
    client, _ = running
    await _channel(client, "work")
    bad = await _set(client, "work", "sleeping")
    assert bad.status_code == 400
    assert "active" in bad.json()["error"]["details"]["allowed"]
    missing = await _set(client, "nope", "done")
    assert missing.status_code == 404


async def test_archived_state_runs_archive_rules(running):
    client, _ = running
    await _channel(client, "work")
    task = (
        await client.post("/api/v1/tasks", json={"channel": "work", "title": "t", "actor": "ada"})
    ).json()["task"]
    await _set(client, "work", "done")

    refused = await _set(client, "work", "archived")
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["task_ids"] == [task["id"]]

    forced = await _set(client, "work", "archived", force=True, note="shipped")
    assert forced.status_code == 200
    body = forced.json()
    assert body["previous_state"] == "done"
    assert body["cancelled_tasks"] == [task["id"]]
    assert body["channel"]["archived_at"] == body["channel"]["state_updated_at"]
    assert body["channel"]["archived_by"] == "ada"

    blocked = await client.post(
        "/api/v1/messages", json={"channel": "work", "author": "ada", "text": "x"}
    )
    assert blocked.json()["error"]["code"] == "channel_archived"

    # Leaving archived for any other state reopens the channel.
    reopened = await _set(client, "work", "review")
    assert reopened.json()["channel"]["archived_at"] is None
    assert reopened.json()["channel"]["state"] == "review"
    ok = await client.post("/api/v1/messages", json={"channel": "work", "author": "ada", "text": "y"})
    assert ok.status_code == 201

    kinds = [e["kind"] for e in await _events(client, "channel.archived,channel.unarchived")]
    assert kinds == ["channel.archived", "channel.unarchived"]

    fleet = await _set(client, "fleet", "archived")
    assert fleet.status_code == 409
    assert (await _set(client, "fleet", "paused")).status_code == 200


async def test_archive_wrappers_agree_with_state(running):
    client, _ = running
    await _channel(client, "work")
    archived = await client.post("/api/v1/channels/work/archive", json={"actor": "ada"})
    assert archived.json()["channel"]["state"] == "archived"
    reopened = await client.post(
        "/api/v1/channels/work/unarchive", json={"actor": "ada", "state": "paused"}
    )
    assert reopened.json()["channel"]["state"] == "paused"
    noop = await client.post("/api/v1/channels/work/unarchive", json={"actor": "ada"})
    assert noop.json()["idempotent"] is True
    bad = await client.post(
        "/api/v1/channels/work/unarchive", json={"actor": "ada", "state": "archived"}
    )
    assert bad.status_code == 400


async def test_list_filters_by_state(running):
    client, _ = running
    for name in ("a1", "b1", "c1"):
        await _channel(client, name)
    await _set(client, "a1", "blocked")
    await _set(client, "c1", "archived")

    def names(response):
        return [c["name"] for c in response.json()["channels"]]

    assert names(await client.get("/api/v1/channels", params={"state": "blocked"})) == ["a1"]
    assert names(await client.get("/api/v1/channels", params={"state": "archived"})) == ["c1"]
    assert names(
        await client.get("/api/v1/channels", params={"state": "active,archived"})
    ) == ["b1", "c1", "fleet"]
    assert "c1" not in names(await client.get("/api/v1/channels"))
    bad = await client.get("/api/v1/channels", params={"state": "nope"})
    assert bad.status_code == 400


async def test_mcp_set_channel_state(running):
    client, _ = running
    await _channel(client, "work")
    result = await mcp_app.set_channel_state("work", "review", "codex", note="ready for eyes")
    assert result["channel"]["state"] == "review"
    listed = await mcp_app.list_channels(state=["review"])
    assert [c["name"] for c in listed["channels"]] == ["work"]
    bad = await mcp_app.set_channel_state("work", "nope", "codex")
    assert bad["status"] == "error" and bad["error"] == "validation_error"
    events = await mcp_app.wait_for_events(kinds=["channel.state_changed"], timeout=0)
    assert events["events"][-1]["payload"]["note"] == "ready for eyes"


V2_DDL = """
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE channels (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    archived_at TEXT, archived_by TEXT
);
INSERT INTO schema_migrations VALUES (1, 'x'), (2, 'x');
INSERT INTO channels (name, topic, created_at) VALUES ('fleet', 'Fleet-wide chat', 'x');
INSERT INTO channels (name, topic, created_at, archived_at, archived_by)
    VALUES ('old', '', 'x', '2026-09-01', 'ada');
"""


async def test_v2_archived_channel_backfills_state(tmp_path):
    path = tmp_path / "v2.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(V2_DDL)
    for _ in range(2):
        db = Database(str(path))
        await db.open()
        await db.close()
    with sqlite3.connect(path) as conn:
        rows = dict(conn.execute("SELECT name, state FROM channels"))
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert rows == {"fleet": "active", "old": "archived"}
    assert versions == list(range(1, SCHEMA_VERSION + 1))
