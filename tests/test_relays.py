"""Relay control plane: config rules, CRUD, versions, heartbeat, scoped tokens, reply routing, MCP."""

from __future__ import annotations

import asyncio

import pytest

from cat_fleet_chat import mcp_app, relay_mcp, relays
from cat_fleet_chat.errors import HubError

ADMIN = {"Authorization": "Bearer test-token"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def make_relay(client, name="bot", platform="discord", **extra):
    body = {"actor": "ann", "name": name, "platform": platform, **extra}
    response = await client.post("/api/v1/relays", json=body, headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


# --- pure rules ------------------------------------------------------------


def test_config_defaults_and_rejects():
    clean = relays.config(None, "slack")
    assert clean["prefix"] == "sl-" and clean["mask"] is True and clean["read_commands"] == "bound"
    assert clean["reactions"] == relays.DEFAULT_REACTIONS
    for bad in (
        {"nope": 1},
        {"prefix": "bad prefix"},
        {"operators": ["has space"]},
        {"mask": "yes"},
        {"read_commands": "everyone"},
        {"reactions": {"📨": "approve {P}"}},
        {"reactions": {"✅": "approve P3"}},
        {"reactions": {"✅": "{0.__class__}"}},
    ):
        with pytest.raises(HubError):
            relays.config(bad, "discord")
    merged = relays.merge_config(clean, {"operators": ["U1"]}, "slack")
    assert merged["operators"] == ["U1"] and merged["prefix"] == "sl-"


def test_binding_rules():
    clean = relays.binding({"external_id": "123", "hub_channel": "fleet"})
    assert clean["kind"] == "channel" and clean["direction"] == "both" and clean["allowed"] == []
    with pytest.raises(HubError):
        relays.binding({"external_id": "123", "hub_channel": "fleet", "kind": "thread"})
    with pytest.raises(HubError):
        relays.binding({"external_id": "bad id", "hub_channel": "fleet"})
    assert relays.binding({"direction": "in"}, partial=True) == {"direction": "in"}


def test_liveness_and_public_shape():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert relays.liveness(None) == "offline"
    assert relays.liveness((now - timedelta(seconds=5)).isoformat()) == "online"
    assert relays.liveness((now - timedelta(seconds=100)).isoformat()) == "stale"
    assert relays.liveness((now - timedelta(hours=1)).isoformat()) == "offline"
    shaped = relays.public_relay({"name": "x", "enabled": False, "token_hash": "h", "last_seen_at": None})
    assert "token_hash" not in shaped and shaped["has_token"] and shaped["state"] == "disabled"


def test_status_scrubbed_and_capped():
    assert relays.status({"guilds": 2, "token": "x"}) == {"guilds": 2}
    with pytest.raises(HubError):
        relays.status({"blob": "x" * 5000})


# --- CRUD and versions -----------------------------------------------------


async def test_create_returns_token_once_and_replies_consumer(authed):
    client, app = authed
    created = await make_relay(client, description="main bot")
    assert created["token"].startswith("rly_")
    assert created["relay"]["has_token"] and "token_hash" not in created["relay"]
    snapshot = (await client.get("/api/v1/relays/bot", headers=ADMIN)).json()
    assert snapshot["replies_consumer"] == "relay-bot-replies"
    assert "token" not in snapshot and snapshot["relay"]["state"] == "offline"
    consumer = (await app.state.hub.get_consumer("relay-bot-replies"))["consumer"]
    assert consumer["destinations"] == ["relay:bot"] and consumer["relay_id"] == created["relay"]["id"]
    again = await client.post("/api/v1/relays", json={"actor": "ann", "name": "bot", "platform": "discord"}, headers=ADMIN)
    assert again.status_code == 409


async def test_update_needs_reviewed_version(authed):
    client, _app = authed
    created = await make_relay(client)
    version = created["relay"]["version"]
    missing = await client.patch("/api/v1/relays/bot", json={"actor": "ann", "enabled": False}, headers=ADMIN)
    assert missing.status_code == 428
    stale = await client.patch(
        "/api/v1/relays/bot", json={"actor": "ann", "enabled": False}, headers={**ADMIN, "If-Match": "99"}
    )
    assert stale.status_code == 412 and stale.json()["error"]["details"]["version"] == version
    ok = await client.patch(
        "/api/v1/relays/bot",
        json={"actor": "ann", "config": {"operators": ["42"]}},
        headers={**ADMIN, "If-Match": str(version)},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["version"] == version + 1
    assert body["relay"]["config"]["operators"] == ["42"]
    assert body["relay"]["config"]["prefix"] == "dc-"
    audit = (await client.get("/api/v1/audit", params={"target": "bot"}, headers=ADMIN)).json()["audit"]
    assert [row["action"] for row in audit][:2] == ["relay.update", "relay.create"]
    assert all("token_hash" not in (row["after"] or {}) for row in audit)


async def test_bindings_and_routes_bump_version(authed):
    client, app = authed
    await make_relay(client)
    hub = app.state.hub
    await hub.create_channel("ops")
    bound = await client.post(
        "/api/v1/relays/bot/bindings",
        json={"actor": "ann", "external_id": "111", "hub_channel": "fleet", "allowed": ["ops"]},
        headers=ADMIN,
    )
    assert bound.status_code == 201, bound.text
    assert bound.json()["version"] == 2
    unknown = await client.post(
        "/api/v1/relays/bot/bindings",
        json={"actor": "ann", "external_id": "112", "hub_channel": "nope"},
        headers=ADMIN,
    )
    assert unknown.status_code == 404
    dup = await client.post(
        "/api/v1/relays/bot/bindings", json={"actor": "ann", "external_id": "111", "hub_channel": "fleet"}, headers=ADMIN
    )
    assert dup.status_code == 409
    binding_id = bound.json()["binding"]["id"]
    patched = await client.patch(
        f"/api/v1/relays/bot/bindings/{binding_id}", json={"actor": "ann", "direction": "in"}, headers=ADMIN
    )
    assert patched.json()["binding"]["direction"] == "in"
    route = await client.post(
        "/api/v1/relays/bot/routes",
        json={"actor": "ann", "external_id": "111", "topics": ["ci.*.done"], "format": "card"},
        headers=ADMIN,
    )
    assert route.status_code == 201, route.text
    shaped = route.json()["route"]
    assert shaped["format"] == "card" and shaped["consumer"]["topics"] == ["ci.*.done"]
    consumer_name = shaped["consumer"]["name"]
    assert consumer_name.startswith("relay-bot-")
    # the route's consumer is managed by the route
    blocked = await client.request("DELETE", f"/api/v1/consumers/{consumer_name}", params={"actor": "ann"}, headers=ADMIN)
    assert blocked.status_code == 409
    updated = await client.patch(
        f"/api/v1/relays/bot/routes/{shaped['id']}",
        json={"actor": "ann", "topics": ["ci.>"], "enabled": False},
        headers=ADMIN,
    )
    assert updated.json()["route"]["consumer"]["topics"] == ["ci.>"]
    assert updated.json()["route"]["enabled"] is False
    snapshot = (await client.get("/api/v1/relays/bot/config", headers=ADMIN)).json()
    assert snapshot["version"] == 5 and len(snapshot["bindings"]) == 1 and len(snapshot["routes"]) == 1
    removed = await client.request(
        "DELETE", f"/api/v1/relays/bot/routes/{shaped['id']}", params={"actor": "ann"}, headers=ADMIN
    )
    assert removed.status_code == 200
    assert (await client.get(f"/api/v1/consumers/{consumer_name}", headers=ADMIN)).status_code == 404
    events = [event["kind"] for event in await hub.events_after(0, channel=None)]
    assert events.count("relay.updated") == 5


async def test_delete_relay_removes_everything(authed):
    client, app = authed
    created = await make_relay(client)
    await client.post(
        "/api/v1/relays/bot/routes", json={"actor": "ann", "external_id": "1", "kinds": ["task.updated"]}, headers=ADMIN
    )
    version = (await client.get("/api/v1/relays/bot", headers=ADMIN)).json()["version"]
    no_confirm = await client.request("DELETE", "/api/v1/relays/bot", params={"actor": "ann"}, headers=ADMIN)
    assert no_confirm.status_code == 428
    gone = await client.request(
        "DELETE", "/api/v1/relays/bot", params={"actor": "ann"}, headers={**ADMIN, "If-Match": str(version)}
    )
    assert gone.status_code == 200, gone.text
    assert (await app.state.hub.list_consumers())["consumers"] == []
    assert (await client.get("/api/v1/relays", headers=ADMIN)).json()["relays"] == []
    assert created["relay"]["id"]


async def test_heartbeat_is_quiet_and_reports_version(authed):
    client, app = authed
    created = await make_relay(client)
    hub = app.state.hub
    token = created["token"]
    head = (await hub.event_cursor())["event_cursor"]
    task = asyncio.create_task(hub.wait_for_events(after_event_id=head, timeout=2))
    await asyncio.sleep(0.05)
    beat = await client.post(
        "/api/v1/relays/bot/heartbeat", json={"status": {"guilds": 1, "token": "drop"}}, headers=bearer(token)
    )
    assert beat.status_code == 200, beat.text
    assert beat.json() == {"name": "bot", "version": 1, "enabled": True}
    await asyncio.sleep(0.1)
    assert not task.done()  # bookkeeping never wakes waiters
    task.cancel()
    shaped = (await client.get("/api/v1/relays/bot", headers=ADMIN)).json()["relay"]
    assert shaped["state"] == "online" and shaped["status"] == {"guilds": 1}


# --- scoped relay tokens ---------------------------------------------------


async def test_relay_token_allowlist(authed):
    client, app = authed
    token = (await make_relay(client))["token"]
    other = (await make_relay(client, name="other"))["token"]
    headers = bearer(token)
    assert (await client.get("/api/v1/relays/bot/config", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/relays/other/config", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/relays", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/audit", headers=headers)).status_code == 403
    assert (await client.post("/api/v1/relays/bot/rotate-token", json={}, headers=headers)).status_code == 403
    assert (await client.get("/api/v1/channels", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/consumers/relay-bot-replies/wait", params={"timeout": 0}, headers=headers)).status_code == 200
    assert (
        await client.get("/api/v1/consumers/relay-other-replies/wait", params={"timeout": 0}, headers=headers)
    ).status_code == 403
    assert (await client.get("/mcp/", headers=headers)).status_code == 403
    assert (await client.get("/api/v1/channels", headers=bearer("rly_wrong"))).status_code == 401
    # a relay posts only as itself
    origin = {"source": "discord", "channel_id": "111", "relay": "bot"}
    ok = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "dc-ann", "text": "hi", "origin": origin},
        headers=headers,
    )
    assert ok.status_code == 201, ok.text
    spoofed = await client.post(
        "/api/v1/messages",
        json={"channel": "fleet", "author": "dc-ann", "text": "hi", "origin": {**origin, "relay": "other"}},
        headers=headers,
    )
    assert spoofed.status_code == 403
    plain = await client.post("/api/v1/messages", json={"channel": "fleet", "author": "x", "text": "hi"}, headers=headers)
    assert plain.status_code == 403
    # rotation kills the old token
    rotated = await client.post("/api/v1/relays/bot/rotate-token", json={"actor": "ann"}, headers=ADMIN)
    assert rotated.json()["token"] != token
    assert (await client.get("/api/v1/relays/bot/config", headers=headers)).status_code == 401
    assert (await client.get("/api/v1/relays/other/config", headers=bearer(other))).status_code == 200
    # a disabled relay's token stops working
    version = (await client.get("/api/v1/relays/other", headers=ADMIN)).json()["version"]
    await client.patch("/api/v1/relays/other", json={"actor": "ann", "enabled": False}, headers={**ADMIN, "If-Match": str(version)})
    assert (await client.get("/api/v1/relays/other/config", headers=bearer(other))).status_code == 401
    assert app.state.hub


# --- reply routing ---------------------------------------------------------


async def test_reply_to_relayed_message_goes_back_through_relay(authed):
    client, app = authed
    created = await make_relay(client)
    hub = app.state.hub
    origin = {"source": "discord", "channel_id": "111", "thread_id": "999", "message_id": "777", "relay": "bot"}
    asked = (
        await client.post(
            "/api/v1/messages",
            json={"channel": "fleet", "author": "dc-ann", "text": "help?", "origin": origin},
            headers=bearer(created["token"]),
        )
    ).json()["message"]
    answer = await hub.post_message("fleet", "agent", "here you go", reply_to=asked["id"])
    assert answer["message"]["destination"] == "relay:bot"
    waited = await hub.consumer_wait("relay-bot-replies", 0)
    assert [event["entity_id"] for event in waited["events"]] == [answer["message"]["id"]]
    target = (
        await client.get(
            f"/api/v1/relays/bot/reply-target/{answer['message']['id']}", headers=bearer(created["token"])
        )
    ).json()["target"]
    assert target == {"external_id": "999", "reply_to_external": "777", "via": "origin"}


async def test_explicit_relay_destination_and_binding_fallback(authed):
    client, app = authed
    await make_relay(client)
    hub = app.state.hub
    await client.post(
        "/api/v1/relays/bot/bindings", json={"actor": "ann", "external_id": "111", "hub_channel": "fleet"}, headers=ADMIN
    )
    sent = await hub.post_message("fleet", "agent", "broadcast", destination_value="relay:bot")
    assert sent["message"]["destination"] == "relay:bot"
    target = await hub.relay_reply_target("bot", sent["message"]["id"])
    assert target["target"] == {"external_id": "111", "reply_to_external": None, "via": "binding"}
    with pytest.raises(HubError) as exc:
        await hub.post_message("fleet", "agent", "x", destination_value="relay:ghost")
    assert exc.value.code == "destination_not_found"
    listed = (await client.get("/api/v1/destinations", headers=ADMIN)).json()["destinations"]
    assert {"name": "relay:bot", "format": "discord"}.items() <= listed[-1].items()


# --- MCP -------------------------------------------------------------------


async def test_mcp_relay_tools(running):
    names = {tool.name for tool in await mcp_app.build_mcp().list_tools()}
    assert {"fleet_relay_list", "fleet_relay_bind", "fleet_relay_route_add"} <= names
    created = await relay_mcp.fleet_relay_create("ann", "tg", "telegram")
    assert "token" not in created and created["relay"]["config"]["prefix"] == "tg-"
    bound = await relay_mcp.fleet_relay_bind("ann", "tg", "-100123", "fleet")
    assert bound["binding"]["external_id"] == "-100123"
    route = await relay_mcp.fleet_relay_route_add("ann", "tg", "-100123", topics=["jobs.>"])
    assert route["route"]["consumer"]["topics"] == ["jobs.>"]
    stale = await relay_mcp.fleet_relay_update("ann", "tg", 1, enabled=False)
    assert stale["status"] == "error" and stale["error"] == "version_conflict"
    fresh = await relay_mcp.fleet_relay_get("tg")
    ok = await relay_mcp.fleet_relay_update("ann", "tg", fresh["version"], description="telegram bridge")
    assert ok["relay"]["description"] == "telegram bridge"
    assert (await relay_mcp.fleet_relay_route_remove("ann", "tg", route["route"]["id"]))["deleted"]
    assert (await relay_mcp.fleet_relay_unbind("ann", "tg", bound["binding"]["id"]))["deleted"]
    assert len((await relay_mcp.fleet_relay_list())["relays"]) == 1
