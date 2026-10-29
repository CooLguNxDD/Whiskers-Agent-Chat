"""cat-fleet-relay core: inbound routing, replies, routes, commands and buttons against a real hub."""

from __future__ import annotations

import pytest

from cat_fleet_chat.relay.adapter import ButtonPress, CommandCall, Inbound
from cat_fleet_chat.relay.core import POISON_AFTER, RelayCore
from cat_fleet_chat.relay.hub_client import HubClient
from cat_fleet_chat.relay.render import parse_button, render

ADMIN = {"Authorization": "Bearer test-token"}
OPERATOR = "900"
CLICKER = "901"
STRANGER = "902"


class FakeAdapter:
    platform = "discord"

    def __init__(self) -> None:
        self.sent: list[tuple[str, object, str | None]] = []
        self.threads: list[tuple[str, str]] = []
        self.fail = 0

    async def send(self, channel_id, message, *, nonce=None):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("platform down")
        self.sent.append((channel_id, message, nonce))
        return str(len(self.sent))

    async def create_thread(self, channel_id, name):
        thread_id = str(5000 + len(self.threads))
        self.threads.append((channel_id, name))
        return thread_id

    def status(self):
        return {"connected": True}


@pytest.fixture
async def relay(authed, tmp_path):
    client, app = authed
    created = await client.post(
        "/api/v1/relays",
        json={
            "actor": "ann",
            "name": "bot",
            "platform": "discord",
            "config": {"operators": [OPERATOR], "action_users": [CLICKER]},
        },
        headers=ADMIN,
    )
    token = created.json()["token"]
    await app.state.hub.create_channel("ops")
    await client.post(
        "/api/v1/relays/bot/bindings",
        json={"actor": "ann", "external_id": "111", "hub_channel": "fleet", "allowed": ["ops"]},
        headers=ADMIN,
    )
    adapter = FakeAdapter()
    core = RelayCore(HubClient(client, "http://testserver", token, "bot"), adapter, cache_path=str(tmp_path / "cache.json"))
    assert await core.load()
    yield core, adapter, app.state.hub, client


def inbound(text, channel="111", parent=None, message="7001", user="903", name="Ann", bot=False):
    return Inbound(channel_id=channel, message_id=message, user_id=user, user_name=name, text=text, parent_id=parent, is_bot=bot)


def call(name, user=OPERATOR, channel="111", parent=None, **args):
    return CommandCall(name=name, args=args, channel_id=channel, user_id=user, user_name="Op", parent_id=parent)


# --- inbound ---------------------------------------------------------------


async def test_inbound_posts_with_relay_origin_and_masks(relay):
    core, _adapter, hub, _client = relay
    result = await core.inbound(inbound("hello ghp_" + "a" * 36))
    assert result.status == "sent" and "masked" in result.note
    message = (await hub.get_messages("fleet"))["messages"][-1]
    assert message["author"] == "dc-Ann"
    assert "ghp_" not in message["text"]
    assert message["origin"] == {
        "source": "discord", "channel_id": "111", "message_id": "7001", "user_id": "903", "author": "Ann", "relay": "bot",
    }
    again = await core.inbound(inbound("hello ghp_" + "a" * 36))
    assert again.hub_message_id == result.hub_message_id  # idempotent by message id


async def test_inbound_ignores_bots_unbound_and_out_only(relay):
    core, _adapter, _hub, _client = relay
    assert (await core.inbound(inbound("x", bot=True))).status == "ignored"
    assert (await core.inbound(inbound("x", channel="999"))).status == "ignored"
    assert (await core.inbound(inbound("   "))).status == "ignored"


async def test_hash_targeting_and_thread_origin(relay):
    core, _adapter, hub, _client = relay
    result = await core.inbound(inbound("deploy #ops now", channel="222", parent="111", message="7002"))
    assert result.hub_channel == "ops"
    message = (await hub.get_messages("ops"))["messages"][-1]
    assert message["text"] == "deploy now"
    assert message["origin"]["thread_id"] == "222" and message["origin"]["channel_id"] == "111"


# --- replies and routes ----------------------------------------------------


async def test_reply_goes_back_into_the_thread(relay):
    core, adapter, hub, _client = relay
    asked = await core.inbound(inbound("help?", channel="222", parent="111", message="7003"))
    answer = await hub.post_message("fleet", "agent", "on it", reply_to=asked.hub_message_id)
    assert answer["message"]["destination"] == "relay:bot"
    assert await core.deliver_once(core.config.replies_consumer, None) is True
    channel, message, _nonce = adapter.sent[-1]
    assert channel == "222" and message.reply_to_external == "7003"
    assert message.text == "**agent**: on it"
    assert await core.deliver_once(core.config.replies_consumer, None) is False  # acked


async def test_route_delivery_is_at_least_once(relay):
    core, adapter, hub, client = relay
    added = await client.post(
        "/api/v1/relays/bot/routes",
        json={"actor": "ann", "external_id": "111", "topics": ["ci.*.done"], "format": "embed"},
        headers=ADMIN,
    )
    await core.reload()
    route = core.config.active_routes()[0]
    assert route["id"] == added.json()["route"]["id"]
    await hub.publish("ci", "ci.web.done", {"ok": True})
    adapter.fail = 1
    assert await core.deliver_once(route["consumer"]["name"], route) is None  # not acked
    assert await core.deliver_once(route["consumer"]["name"], route) is True
    assert len(adapter.sent) == 1 and adapter.sent[0][1].title == "📣 ci.web.done"
    assert await core.deliver_once(route["consumer"]["name"], route) is False


async def test_poison_event_is_skipped(relay):
    core, adapter, hub, client = relay
    await client.post(
        "/api/v1/relays/bot/routes", json={"actor": "ann", "external_id": "111", "topics": ["x.>"]}, headers=ADMIN
    )
    await core.reload()
    route = core.config.active_routes()[0]
    await hub.publish("ci", "x.y", {})
    adapter.fail = POISON_AFTER
    for _ in range(POISON_AFTER - 1):
        assert await core.deliver_once(route["consumer"]["name"], route) is None
    assert await core.deliver_once(route["consumer"]["name"], route) is True
    assert core.stats["skipped"] == 1 and adapter.sent == []


async def test_route_never_echoes_own_inbound(relay):
    core, adapter, _hub, client = relay
    await client.post(
        "/api/v1/relays/bot/routes",
        json={"actor": "ann", "external_id": "111", "kinds": ["message.created"]},
        headers=ADMIN,
    )
    await core.reload()
    route = core.config.active_routes()[0]
    await core.inbound(inbound("mine"))
    assert await core.deliver_once(route["consumer"]["name"], route) is True
    assert adapter.sent == []


# --- commands --------------------------------------------------------------


async def test_command_permissions(relay):
    core, _adapter, _hub, _client = relay
    assert (await core.command(call("status", user=STRANGER))).ok
    assert not (await core.command(call("status", user=STRANGER, channel="999"))).ok
    assert not (await core.command(call("bind", user=STRANGER, channel="999", channel_name="ops"))).ok
    assert not (await core.command(call("nope"))).ok
    help_text = (await core.command(call("help", user=STRANGER))).text
    assert "subscribe" not in help_text and "tasks" in help_text


async def test_bind_subscribe_and_unsubscribe(relay):
    core, _adapter, _hub, _client = relay
    bound = await core.command(call("bind", channel="333", **{"channel_name": None}, ))
    assert not bound.ok  # missing channel
    bound = await core.command(CommandCall("bind", {"channel": "#ops"}, "333", OPERATOR, "Op"))
    assert bound.ok and core.config.binding_for("333")["hub_channel"] == "ops"
    sub = await core.command(CommandCall("subscribe", {"what": "task.updated", "format": "card"}, "333", OPERATOR, "Op"))
    assert sub.ok, sub.text
    route = core.config.routes[-1]
    assert route["consumer"]["kinds"] == ["task.updated"] and route["consumer"]["exclude_authors"] == ["dc-*"]
    listed = await core.command(CommandCall("routes", {}, "333", OPERATOR, "Op"))
    assert f"route {route['id']}" in listed.text
    gone = await core.command(CommandCall("unsubscribe", {"route": str(route["id"])}, "333", OPERATOR, "Op"))
    assert gone.ok and core.config.routes == ()
    assert (await core.command(CommandCall("unbind", {}, "333", OPERATOR, "Op"))).ok
    assert core.config.binding_for("333") is None


async def test_thread_command_binds_a_task_thread(relay):
    core, adapter, hub, _client = relay
    task = (await hub.create_task("fleet", "Fix the build", actor="ann"))["task"]
    reply = await core.command(CommandCall("thread", {"target": f"task:{task['id']}"}, "111", OPERATOR, "Op"))
    assert reply.ok, reply.text
    assert adapter.threads == [("111", f"Task #{task['id']}")]
    binding = core.config.binding_for("5000")
    assert binding["kind"] == "thread" and binding["task_id"] == task["id"]
    route = [r for r in core.config.routes if r["external_id"] == "5000"][0]
    assert route["consumer"]["task_ids"] == [task["id"]] and route["format"] == "card"


async def test_tasks_proposals_and_post(relay):
    core, _adapter, hub, _client = relay
    await hub.create_task("fleet", "Write docs", actor="ann")
    assert "Write docs" in (await core.command(call("tasks"))).text
    await hub.create_proposal("builder", "demo", "Ship relay", "Summary", 3)
    assert "Ship relay" in (await core.command(call("proposals"))).text
    posted = await core.command(CommandCall("post", {"channel": "ops", "text": "hi from discord"}, "111", STRANGER, "Sam"))
    assert posted.ok, posted.text
    assert (await hub.get_messages("ops"))["messages"][-1]["author"] == "dc-Sam"
    denied = await core.command(CommandCall("post", {"channel": "secret", "text": "x"}, "111", STRANGER, "Sam"))
    assert not denied.ok


# --- buttons ---------------------------------------------------------------


async def test_task_buttons_follow_versions(relay):
    core, _adapter, hub, _client = relay
    task = (await hub.create_task("fleet", "Card task", actor="ann"))["task"]
    event = {"id": 1, "kind": "task.updated", "channel": "fleet", "payload": task}
    claim = render(event, "card").buttons[0]
    assert claim.label == "Claim"
    assert not (await core.button(ButtonPress(claim.custom_id, "111", STRANGER, "Sam"))).ok
    claimed = await core.button(ButtonPress(claim.custom_id, "111", CLICKER, "Cleo"))
    assert claimed.ok and "claimed" in claimed.text
    stale = await core.button(ButtonPress(claim.custom_id, "111", CLICKER, "Cleo"))
    assert stale.ok or "version" in stale.text  # a repeated claim is idempotent or refused, never double-applied


async def test_proposal_buttons_post_commands(relay):
    core, _adapter, hub, _client = relay
    hub.human_handles = frozenset({"dc-cleo"})
    proposal = (await hub.create_proposal("builder", "demo", "Ship relay", "Summary", 3))["proposal"]
    event = {"id": 1, "kind": "proposal_created", "channel": None, "payload": proposal}
    approve, reject = render(event, "card").buttons
    assert not (await core.button(ButtonPress(reject.custom_id, "111", CLICKER, "cleo"))).ok  # needs a reason
    ok = await core.button(ButtonPress(approve.custom_id, "111", CLICKER, "cleo"))
    assert ok.ok, ok.text
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "accepted"


def test_button_ids_reject_foreign_shapes():
    assert parse_button("cf1:approve:proposal:P3").verb == "approve"
    for bad in ("cf1:approve:task:1.1", "cf1:claim:task:1", "cf2:up:feed:1", "cf1:up:feed:0", "x", None):
        assert parse_button(bad) is None


async def test_config_cache_survives_hub_outage(relay, tmp_path):
    core, adapter, _hub, _client = relay

    class DownHub:
        relay = "bot"

        async def config(self):
            return False, "hub unreachable"

    offline = RelayCore(DownHub(), adapter, cache_path=str(tmp_path / "cache.json"))
    assert await offline.load()
    assert offline.config.binding_for("111")["hub_channel"] == "fleet"
    empty = RelayCore(DownHub(), adapter, cache_path=str(tmp_path / "none.json"))
    assert not await empty.load()
