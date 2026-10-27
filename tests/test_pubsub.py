"""Topic pub/sub: pattern rules, hub rules, wait delivery, SSE, webhooks, REST, MCP, migration."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from cat_fleet_chat import mcp_app, pubsub
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import agent_relevant
from cat_fleet_chat.webhooks import event_line, matches


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


# --- pure rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "topic", "expected"),
    [
        ("build.done", "build.done", True),
        ("build.done", "build.done.x", False),
        ("build.*", "build.done", True),
        ("build.*", "build", False),
        ("build.*", "build.a.b", False),
        ("build.*.done", "build.whiskers.done", True),
        ("build.>", "build.whiskers.done", True),
        ("build.>", "build", False),
        (">", "anything.at.all", True),
        ("*", "one", True),
        ("*", "one.two", False),
    ],
)
def test_pattern_matching(pattern, topic, expected):
    assert pubsub.matches(pubsub.pattern(pattern), pubsub.topic(topic)) is expected


@pytest.mark.parametrize("bad", ["", "Build", "a..b", ".a", "a.", "a.*", "a.>", "a b", "x" * 201, ".".join("a" * 9)])
def test_topic_rejects(bad):
    with pytest.raises(HubError) as exc:
        pubsub.topic(bad)
    assert exc.value.code == "validation_error"


@pytest.mark.parametrize("bad", ["a.>.b", "a.**", "a.b>", "", "a..*"])
def test_pattern_rejects(bad):
    with pytest.raises(HubError):
        pubsub.pattern(bad)


def test_literal_prefix_and_data_cap():
    assert pubsub.literal_prefix("build.*.done") == "build"
    assert pubsub.literal_prefix("build.whiskers.>") == "build.whiskers"
    assert pubsub.literal_prefix("*.done") == ""
    assert pubsub.data({"ok": True, "n": [1, 2]}) == {"ok": True, "n": [1, 2]}
    assert pubsub.data(None) is None
    with pytest.raises(HubError):
        pubsub.data("x" * 16_001)
    with pytest.raises(HubError):
        pubsub.data(float("nan"))


def test_agent_relevant_topic_rules():
    event = {"kind": "topic.published", "payload": {"topic": "ci.build.done", "publisher": "ada"}}
    assert agent_relevant(event, None)
    assert agent_relevant(event, "bob", ["ci.>"])
    assert not agent_relevant(event, "bob", ["ci.deploy.>"])
    assert not agent_relevant(event, "bob")
    assert not agent_relevant(event, "ada", ["ci.>"])  # never your own publish


# --- hub rules -------------------------------------------------------------


async def test_subscribe_publish_and_history(running):
    _client, app = running
    hub = app.state.hub
    first = await hub.subscribe("bob", "ci.*.done")
    assert first["created"] is True and first["subscriptions"] == ["ci.*.done"]
    again = await hub.subscribe("bob", "ci.*.done")
    assert again["created"] is False and again["subscriptions"] == ["ci.*.done"]
    await hub.subscribe("carol", "ci.>")
    await hub.subscribe("ada", "ci.>")

    published = await hub.publish("ada", "ci.whiskers.done", {"ok": True})
    pub = published["publication"]
    assert pub["topic"] == "ci.whiskers.done" and pub["data"] == {"ok": True}
    assert pub["publisher"] == "ada"
    assert published["subscribers"] == ["bob", "carol"]  # publisher excluded
    assert (await hub.publish("ada", "ci.whiskers.started"))["subscribers"] == ["carol"]
    await hub.publish("ada", "deploy.prod", [1, 2])

    history = await hub.list_publications("ci.>")
    assert [p["topic"] for p in history["publications"]] == ["ci.whiskers.done", "ci.whiskers.started"]
    assert history["has_more"] is False
    wild = await hub.list_publications("*.whiskers.done")
    assert [p["id"] for p in wild["publications"]] == [pub["id"]]
    paged = await hub.list_publications(None, limit=1)
    assert len(paged["publications"]) == 1 and paged["has_more"] is True
    rest = await hub.list_publications(None, since_id=paged["cursor"])
    assert [p["topic"] for p in rest["publications"]] == ["ci.whiskers.started", "deploy.prod"]

    listed = await hub.list_subscriptions(topic="ci.whiskers.done")
    assert {(r["subscriber"], r["pattern"]) for r in listed["subscriptions"]} == {
        ("ada", "ci.>"),
        ("bob", "ci.*.done"),
        ("carol", "ci.>"),
    }
    assert [r["pattern"] for r in (await hub.list_subscriptions("bob"))["subscriptions"]] == ["ci.*.done"]

    gone = await hub.unsubscribe("bob", "ci.*.done")
    assert gone["removed"] is True and gone["subscriptions"] == []
    assert (await hub.unsubscribe("bob", "ci.*.done"))["removed"] is False


async def test_publish_idempotency(running):
    _client, app = running
    hub = app.state.hub
    one = await hub.publish("ada", "jobs.new", {"n": 1}, client_request_id="k1")
    two = await hub.publish("ada", "jobs.new", {"n": 1}, client_request_id="k1")
    assert one == two
    assert len((await hub.list_publications("jobs.new"))["publications"]) == 1
    with pytest.raises(HubError) as conflict:
        await hub.publish("ada", "jobs.new", {"n": 2}, client_request_id="k1")
    assert conflict.value.code == "idempotency_conflict"


async def test_subscription_cap(running, monkeypatch):
    _client, app = running
    hub = app.state.hub
    monkeypatch.setattr(pubsub, "SUBSCRIPTIONS_MAX", 2)
    await hub.subscribe("bob", "a")
    await hub.subscribe("bob", "b")
    assert (await hub.subscribe("bob", "a"))["created"] is False  # existing pattern is fine
    with pytest.raises(HubError) as capped:
        await hub.subscribe("bob", "c")
    assert capped.value.code == "limit_exceeded"


# --- delivery --------------------------------------------------------------


async def test_wait_for_events_wakes_subscriber_only(running):
    _client, app = running
    hub = app.state.hub
    await hub.subscribe("bob", "ci.>")
    cursor = (await hub.event_cursor())["event_cursor"]

    pending = asyncio.create_task(hub.wait_for_events("bob", cursor, timeout=5))
    await _parked(hub)
    await hub.publish("ada", "deploy.prod", {"skip": True})  # not subscribed
    await hub.publish("bob", "ci.self", {})  # own publish
    published = await hub.publish("ada", "ci.build.done", {"sha": "abc"})
    woke = await asyncio.wait_for(pending, 3)
    assert [e["kind"] for e in woke["events"]] == ["topic.published"]
    payload = woke["events"][0]["payload"]
    assert payload == {
        "id": published["publication"]["id"],
        "topic": "ci.build.done",
        "publisher": "ada",
        "data": {"sha": "abc"},
    }
    assert woke["events"][0]["entity_id"] == published["publication"]["id"]


async def test_subscribe_while_parked_takes_effect(running):
    _client, app = running
    hub = app.state.hub
    cursor = (await hub.event_cursor())["event_cursor"]
    pending = asyncio.create_task(hub.wait_for_events("bob", cursor, timeout=5))
    await _parked(hub)
    await hub.subscribe("bob", "news.*")
    await hub.publish("ada", "news.today", "hello")
    woke = await asyncio.wait_for(pending, 3)
    assert [e["payload"]["data"] for e in woke["events"]] == ["hello"]


async def test_sse_delivers_subscribed_topics(running):
    """Raw ASGI, as in test_features: httpx's ASGI transport buffers an open stream."""
    _client, app = running
    hub = app.state.hub
    await hub.subscribe("codex", "ci.>")
    await hub.publish("ada", "other.topic", 1)
    await hub.publish("ada", "ci.done", 2)
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
        "query_string": b"after_event_id=0&agent=codex&kinds=topic.published",
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
    assert [f["payload"]["topic"] for f in frames] == ["ci.done"]


# --- webhooks --------------------------------------------------------------


def test_webhook_topic_events_are_opt_in():
    event = {
        "id": 4,
        "kind": "topic.published",
        "channel_id": None,
        "channel": None,
        "entity_id": 1,
        "payload": {"id": 1, "topic": "ci.done", "publisher": "ada", "data": {"ok": True}},
        "created_at": "t",
    }
    line = event_line(event)
    assert "ci.done" in line and "ada" in line and '"ok":true' in line
    base = {"name": "d", "kinds": [], "channels": [], "mentions": [], "exclude_authors": []}
    assert not matches(base, event)  # machine traffic stays out of chat hooks by default
    assert matches({**base, "kinds": ["topic.published"]}, event)
    assert not matches({**base, "kinds": ["topic.published"], "exclude_authors": ["ad*"]}, event)


# --- REST ------------------------------------------------------------------


async def test_rest_pubsub_flow(running):
    client, _app = running
    sub = await client.post("/api/v1/subscriptions/bob", json={"pattern": "ci.>"})
    assert sub.status_code == 201 and sub.json()["created"] is True
    again = await client.post("/api/v1/subscriptions/bob", json={"pattern": "ci.>"})
    assert again.status_code == 200 and again.json()["created"] is False

    sent = await client.post(
        "/api/v1/topics/ci.build.done/publish", json={"publisher": "ada", "data": {"ok": 1}}
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["subscribers"] == ["bob"]

    notes = await client.get(
        "/api/v1/notifications", params={"agent": "bob", "timeout": 0, "kinds": "topic.published"}
    )
    assert [e["payload"]["data"] for e in notes.json()["events"]] == [{"ok": 1}]

    history = await client.get("/api/v1/publications", params={"pattern": "ci.*.done"})
    assert [p["topic"] for p in history.json()["publications"]] == ["ci.build.done"]
    listed = await client.get("/api/v1/subscriptions", params={"agent": "bob"})
    assert [r["pattern"] for r in listed.json()["subscriptions"]] == ["ci.>"]

    removed = await client.request("DELETE", "/api/v1/subscriptions/bob", params={"pattern": "ci.>"})
    assert removed.status_code == 200 and removed.json()["removed"] is True

    bad_topic = await client.post("/api/v1/topics/Bad/publish", json={"publisher": "ada"})
    assert bad_topic.status_code == 400
    bad_pattern = await client.post("/api/v1/subscriptions/bob", json={"pattern": "a.>.b"})
    assert bad_pattern.status_code == 400


async def test_rest_pubsub_requires_auth(authed):
    client, _app = authed
    assert (await client.post("/api/v1/topics/a/publish", json={})).status_code == 401
    assert (await client.post("/api/v1/subscriptions/bob", json={"pattern": "a"})).status_code == 401
    assert (await client.get("/api/v1/subscriptions")).status_code == 401
    assert (await client.get("/api/v1/publications")).status_code == 401


# --- MCP -------------------------------------------------------------------


async def test_mcp_pubsub_tools(running):
    names = {tool.name for tool in await mcp_app.build_mcp().list_tools()}
    assert {
        "fleet_publish",
        "fleet_subscribe",
        "fleet_unsubscribe",
        "fleet_list_subscriptions",
        "fleet_list_publications",
    } <= names
    assert (await mcp_app.fleet_subscribe("bob", "jobs.*"))["created"] is True
    sent = await mcp_app.fleet_publish("ada", "jobs.new", {"id": 7})
    assert sent["subscribers"] == ["bob"]
    waited = await mcp_app.wait_for_events("bob", 0, kinds=["topic.published"], timeout=0)
    assert [e["payload"]["data"] for e in waited["events"]] == [{"id": 7}]
    assert [p["id"] for p in (await mcp_app.fleet_list_publications("jobs.>"))["publications"]] == [
        sent["publication"]["id"]
    ]
    assert (await mcp_app.fleet_list_subscriptions("bob"))["subscriptions"][0]["pattern"] == "jobs.*"
    assert (await mcp_app.fleet_unsubscribe("bob", "jobs.*"))["removed"] is True
    bad = await mcp_app.fleet_publish("ada", "Not.Valid", None)
    assert bad["status"] == "error" and bad["error"] == "validation_error"


# --- migration -------------------------------------------------------------


async def test_v13_database_upgrades_on_open(tmp_path):
    path = tmp_path / "v13.sqlite"
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE publications")
        conn.execute("DROP TABLE topic_subscriptions")
        conn.execute("DELETE FROM schema_migrations WHERE version > 13")

    for _ in range(2):  # the second open must be a no-op
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert SCHEMA_VERSION >= 14
    assert {"publications", "topic_subscriptions"} <= tables
    assert versions == list(range(1, SCHEMA_VERSION + 1))


# --- subscription events and newest-first history ---------------------------


async def test_subscription_changes_are_events(running):
    _client, app = running
    hub = app.state.hub
    cursor = (await hub.event_cursor())["event_cursor"]
    await hub.subscribe("bob", "ci.>")
    await hub.subscribe("bob", "ci.>")  # repeat: no second event
    await hub.unsubscribe("bob", "ci.>")
    await hub.unsubscribe("bob", "ci.>")  # repeat: no second event
    page = await hub.wait_for_events(None, cursor, timeout=0)
    assert [(e["kind"], e["payload"]) for e in page["events"]] == [
        ("topic.subscribed", {"subscriber": "bob", "pattern": "ci.>"}),
        ("topic.unsubscribed", {"subscriber": "bob", "pattern": "ci.>"}),
    ]
    # Not relevant to any agent's wait, the subscriber's included.
    assert not any(agent_relevant(e, "bob", ["ci.>"]) for e in page["events"])
    mine = await hub.wait_for_events("bob", cursor, timeout=0)
    assert mine["events"] == [] and mine["cursor"] == page["cursor"]


def test_webhook_subscription_events_opt_in():
    event = {
        "id": 5,
        "kind": "topic.subscribed",
        "channel": None,
        "payload": {"subscriber": "bob", "pattern": "ci.>"},
    }
    assert "bob" in event_line(event) and "ci.>" in event_line(event)
    base = {"name": "d", "kinds": [], "channels": [], "mentions": [], "exclude_authors": []}
    assert not matches(base, event)
    assert matches({**base, "kinds": ["topic.subscribed"]}, event)


async def test_publications_newest_first(running):
    client, app = running
    hub = app.state.hub
    ids = [(await hub.publish("ada", f"t.n{i}", i))["publication"]["id"] for i in range(5)]
    await hub.publish("ada", "other", 0)
    first = await hub.list_publications("t.>", limit=2, order="desc")
    assert [p["id"] for p in first["publications"]] == [ids[4], ids[3]]
    assert first["has_more"] is True and first["cursor"] == ids[3]
    rest = await hub.list_publications("t.>", order="desc", before_id=first["cursor"])
    assert [p["id"] for p in rest["publications"]] == [ids[2], ids[1], ids[0]]
    assert rest["has_more"] is False
    via_rest = await client.get("/api/v1/publications", params={"order": "desc", "limit": 1})
    assert via_rest.json()["publications"][0]["topic"] == "other"
    bad = await client.get("/api/v1/publications", params={"order": "sideways"})
    assert bad.status_code == 400
