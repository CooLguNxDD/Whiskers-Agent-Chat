"""Durable consumers and the audit log: filter rules, wait/ack/seek, REST, MCP, migration."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from cat_fleet_chat import consumers, mcp_app, relay_mcp
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.webhooks import matches as hook_matches


async def _until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _parked(hub, count: int = 1) -> None:
    assert await _until(lambda: hub.waiters.waiter_count >= count)


def _event(kind, payload=None, channel=None, event_id=1):
    return {"id": event_id, "kind": kind, "channel": channel, "payload": payload or {}}


# --- pure rules ------------------------------------------------------------


def test_filters_clean_and_reject():
    clean = consumers.filters(
        {"kinds": "message.created,task.updated", "topics": ["ci.*.done"], "task_ids": ["3", 3]}
    )
    assert clean["kinds"] == ["message.created", "task.updated"]
    assert clean["topics"] == ["ci.*.done"]
    assert clean["task_ids"] == [3]
    assert clean["channels"] == []
    for bad in (
        {"kinds": ["nope"]},
        {"topics": ["a.>.b"]},
        {"channels": ["Bad Name"]},
        {"task_ids": [0]},
        {"task_ids": [True]},
        {"kinds": ["message.created"] * (consumers.FILTER_MAX + 1)},
    ):
        with pytest.raises(HubError):
            consumers.filters(bad)
    assert consumers.filters({"topics": ["x"]}, partial=True) == {"topics": ["x"]}


def test_matching_rules():
    topic_only = {"topics": ["ci.*.done"]}
    assert consumers.matches(topic_only, _event("topic.published", {"topic": "ci.web.done"}))
    assert not consumers.matches(topic_only, _event("topic.published", {"topic": "ci.web.start"}))
    # topics with no kinds limit the consumer to publishes
    assert not consumers.matches(topic_only, _event("message.created", {"author": "a"}))
    # with kinds, topics only constrain publishes
    mixed = {"kinds": ["message.created", "topic.published"], "topics": ["ci.>"]}
    assert consumers.matches(mixed, _event("message.created", {"author": "a"}))
    task = {"kinds": ["task.updated"], "task_ids": [7]}
    assert consumers.matches(task, _event("task.updated", {"id": 7}))
    assert not consumers.matches(task, _event("task.updated", {"id": 8}))
    chan = {"channels": ["ops"]}
    assert consumers.matches(chan, _event("message.created", channel="ops"))
    assert not consumers.matches(chan, _event("message.created", channel="fleet"))
    dest = {"destinations": ["relay:bot"]}
    assert consumers.matches(dest, _event("message.created", {"destination": "relay:bot"}))
    assert not consumers.matches(dest, _event("message.created", {"destination": None}))
    assert not consumers.matches(dest, _event("task.updated", {}))
    no_dc = {"exclude_authors": ["dc-*"]}
    assert not consumers.matches(no_dc, _event("message.created", {"author": "dc-ann"}))
    assert consumers.matches(no_dc, _event("message.created", {"author": "bob"}))
    assert consumers.matches({}, _event("mail", {"sender": "a"}))


def test_audit_value_scrubs_and_caps():
    text = consumers.audit_value({"name": "x", "token_hash": "abc", "nested": [{"secret": "s", "ok": 1}]})
    assert "abc" not in text and '"secret"' not in text and '"ok":1' in text
    big = consumers.audit_value({"blob": "x" * 20_000})
    assert len(big) <= consumers.AUDIT_VALUE_MAX and '"truncated": true' in big


def test_consumer_kinds_are_opt_in_for_webhooks():
    hook = {"name": "h", "kinds": [], "channels": [], "mentions": [], "exclude_authors": []}
    assert not hook_matches(hook, _event("consumer.seeked", {"actor": "a"}))


# --- hub rules -------------------------------------------------------------


async def test_wait_delivers_until_acked(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "ann-ops", "channels": ["fleet"]})
    first = await hub.post_message("fleet", "bob", "one")
    result = await hub.consumer_wait("ann-ops", 0)
    assert [event["entity_id"] for event in result["events"]] == [first["message"]["id"]]
    # not acked: the same events come again
    again = await hub.consumer_wait("ann-ops", 0)
    assert [event["id"] for event in again["events"]] == [event["id"] for event in result["events"]]
    ack = await hub.consumer_ack("ann-ops", result["cursor"])
    assert ack["acked"] is True
    empty = await hub.consumer_wait("ann-ops", 0)
    assert empty["events"] == [] and empty["timed_out"] is False
    # never backward, clamped to the head
    assert (await hub.consumer_ack("ann-ops", 0))["cursor"] == ack["cursor"]
    assert (await hub.consumer_ack("ann-ops", 10_000))["cursor"] == ack["head"]


async def test_start_now_skips_history_and_beginning_replays(running):
    _client, app = running
    hub = app.state.hub
    await hub.post_message("fleet", "bob", "old")
    await hub.create_consumer("ann", {"name": "fresh", "kinds": ["message.created"]})
    await hub.create_consumer("ann", {"name": "replay", "kinds": ["message.created"], "start": "beginning"})
    assert (await hub.consumer_wait("fresh", 0))["events"] == []
    assert len((await hub.consumer_wait("replay", 0))["events"]) == 1


async def test_parked_wait_wakes_on_match_only(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "ci", "topics": ["ci.*.done"]})
    task = asyncio.create_task(hub.consumer_wait("ci", 5))
    await _parked(hub)
    await hub.publish("bot", "ci.web.start", {"n": 1})
    await asyncio.sleep(0.1)
    assert not task.done()
    await hub.publish("bot", "ci.web.done", {"n": 2})
    result = await asyncio.wait_for(task, 3)
    assert [event["payload"]["topic"] for event in result["events"]] == ["ci.web.done"]
    listed = (await hub.list_consumers())["consumers"][0]
    assert listed["armed"] is True


async def test_noise_is_skipped(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "quiet", "channels": ["ops"]})
    for n in range(3):
        await hub.post_message("fleet", "bob", f"noise {n}")
    result = await hub.consumer_wait("quiet", 0)
    assert result["events"] == []
    stored = (await hub.get_consumer("quiet"))["consumer"]
    assert stored["cursor"] == result["cursor"] and stored["behind"] == 0


async def test_seek_replays_and_is_audited(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "c1", "kinds": ["message.created"]})
    posted = await hub.post_message("fleet", "bob", "hello")
    got = await hub.consumer_wait("c1", 0)
    await hub.consumer_ack("c1", got["cursor"])
    seek = await hub.consumer_seek("ann", "c1", after_event_id=0)
    assert seek["cursor"] == 0 and seek["previous"] == got["cursor"]
    again = await hub.consumer_wait("c1", 0)
    assert again["events"][0]["entity_id"] == posted["message"]["id"]
    # by time: before everything
    assert (await hub.consumer_seek("ann", "c1", at="2000-01-01T00:00:00Z"))["cursor"] == 0
    with pytest.raises(HubError):
        await hub.consumer_seek("ann", "c1")
    with pytest.raises(HubError):
        await hub.consumer_seek("ann", "c1", after_event_id=1, at="2000-01-01T00:00:00Z")
    audit = (await hub.list_audit(target_kind="consumer", target="c1"))["audit"]
    assert [row["action"] for row in audit] == ["seek", "seek", "create"]
    assert audit[0]["actor"] == "ann"


async def test_create_update_delete_rules(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "c2"})
    with pytest.raises(HubError) as exc:
        await hub.create_consumer("ann", {"name": "c2"})
    assert exc.value.status == 409
    updated = await hub.update_consumer("bob", "c2", {"channels": ["ops"], "owner": "bob"})
    assert updated["consumer"]["channels"] == ["ops"] and updated["consumer"]["owner"] == "bob"
    assert (await hub.list_consumers(owner="bob"))["consumers"][0]["name"] == "c2"
    await hub.delete_consumer("bob", "c2")
    with pytest.raises(HubError) as exc:
        await hub.get_consumer("c2")
    assert exc.value.status == 404


async def test_relay_owned_consumers_are_managed(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "route-1"}, relay_id=1)
    with pytest.raises(HubError) as exc:
        await hub.delete_consumer("ann", "route-1")
    assert exc.value.code == "managed_by_relay"
    await hub.delete_consumer("ann", "route-1", internal=True)


async def test_consumer_events_logged(running):
    _client, app = running
    hub = app.state.hub
    await hub.create_consumer("ann", {"name": "c3"})
    kinds = [event["kind"] for event in await hub.events_after(0, channel=None)]
    assert "consumer.created" in kinds


# --- REST ------------------------------------------------------------------


async def test_rest_flow(running):
    client, _app = running
    response = await client.post("/api/v1/consumers", json={"actor": "ann", "name": "r1", "kinds": ["message.created"]})
    assert response.status_code == 201, response.text
    await client.post("/api/v1/messages", json={"channel": "fleet", "author": "bob", "text": "hi"})
    waited = (await client.get("/api/v1/consumers/r1/wait", params={"timeout": 0})).json()
    assert len(waited["events"]) == 1
    acked = await client.post("/api/v1/consumers/r1/ack", json={"cursor": waited["cursor"]})
    assert acked.json()["acked"] is True
    seek = await client.post("/api/v1/consumers/r1/seek", json={"actor": "ann", "after_event_id": 0})
    assert seek.status_code == 200
    patched = await client.patch("/api/v1/consumers/r1", json={"actor": "ann", "channels": ["fleet"]})
    assert patched.json()["consumer"]["channels"] == ["fleet"]
    listed = (await client.get("/api/v1/consumers")).json()
    assert [row["name"] for row in listed["consumers"]] == ["r1"]
    audit = (await client.get("/api/v1/audit", params={"target": "r1"})).json()["audit"]
    assert {row["action"] for row in audit} == {"create", "seek", "update"}
    missing_actor = await client.post("/api/v1/consumers", json={"name": "r2"})
    assert missing_actor.status_code == 400
    deleted = await client.request("DELETE", "/api/v1/consumers/r1", params={"actor": "ann"})
    assert deleted.status_code == 200
    assert (await client.get("/api/v1/consumers/r1")).status_code == 404


async def test_rest_requires_token(authed):
    client, _app = authed
    assert (await client.get("/api/v1/consumers")).status_code == 401
    assert (await client.get("/api/v1/audit")).status_code == 401


# --- MCP -------------------------------------------------------------------


async def test_mcp_tools(running):
    tools = {tool.name for tool in await mcp_app.build_mcp().list_tools()}
    assert {fn.__name__ for fn in relay_mcp.RELAY_TOOLS} <= tools
    created = await relay_mcp.fleet_consumer_create("ann", "m1", topics=["jobs.>"])
    assert created["consumer"]["topics"] == ["jobs.>"]
    _client, app = running
    await app.state.hub.publish("bot", "jobs.new", {"x": 1})
    waited = await relay_mcp.fleet_consumer_wait("m1", 0)
    assert waited["notice"].startswith("UNTRUSTED DATA") and len(waited["events"]) == 1
    assert (await relay_mcp.fleet_consumer_ack("m1", waited["cursor"]))["acked"] is True
    assert (await relay_mcp.fleet_consumer_update("ann", "m1", channels=[]))["consumer"]["channels"] == []
    bad = await relay_mcp.fleet_consumer_create("ann", "bad name")
    assert bad["status"] == "error" and bad["error"] == "validation_error"
    assert (await relay_mcp.fleet_audit_list(target="m1"))["audit"]


# --- migration -------------------------------------------------------------


async def test_v14_database_upgrades_on_open(tmp_path):
    path = tmp_path / "v14.sqlite"
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        for table in ("relay_routes", "relay_bindings", "relays", "consumers", "audit"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version > 14")

    for _ in range(2):  # the second open must be a no-op
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert SCHEMA_VERSION >= 15
    assert {"consumers", "audit", "relays", "relay_bindings", "relay_routes"} <= tables
    assert versions == list(range(1, SCHEMA_VERSION + 1))
