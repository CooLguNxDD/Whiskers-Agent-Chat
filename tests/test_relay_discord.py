"""cat-fleet-relay Discord adapter and CLI, with a fake ``discord`` module (no discord.py, no network)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import httpx
import pytest

from cat_fleet_chat.discord_relay import MessageRefs
from cat_fleet_chat.relay import cli
from cat_fleet_chat.relay.core import RelayCore
from cat_fleet_chat.relay.discord_adapter import DiscordAdapter, register_commands
from cat_fleet_chat.relay.hub_client import HubClient
from cat_fleet_chat.relay.render import Button, Rendered

ADMIN = {"Authorization": "Bearer test-token"}


# --- a fake discord module -------------------------------------------------


class Embed:
    def __init__(self, title=None, description=None, color=None):
        self.title, self.description, self.color, self.fields, self.footer = title, description, color, [], None

    def add_field(self, name, value, inline):
        self.fields.append((name, value))

    def set_footer(self, text):
        self.footer = text


class View:
    def __init__(self, timeout):
        self.items = []

    def add_item(self, item):
        self.items.append(item)


class Modal:
    def __init__(self, title, custom_id, timeout):
        self.title, self.custom_id, self.items = title, custom_id, []

    def add_item(self, item):
        self.items.append(item)


def fake_discord():
    return NS(
        Embed=Embed,
        ui=NS(View=View, Button=lambda **kw: NS(**kw), Modal=Modal, TextInput=lambda **kw: NS(**kw)),
        ButtonStyle=NS(primary="P", secondary="S", success="G", danger="R"),
        AllowedMentions=NS(none=lambda: "NONE"),
        MessageReference=lambda **kw: NS(**kw),
        ChannelType=NS(public_thread="public"),
        TextStyle=NS(paragraph="para"),
    )


class Channel:
    def __init__(self, ident, parent_id=None):
        self.id, self.parent_id, self.sent, self.threads = ident, parent_id, [], []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return NS(id=9000 + len(self.sent))

    async def create_thread(self, **kwargs):
        self.threads.append(kwargs)
        return NS(id=4242)


class Client:
    def __init__(self):
        self.user = NS(id=1, name="bot")
        self.guilds = [1, 2]
        self.channels = {111: Channel(111)}

    def get_channel(self, ident):
        return self.channels.get(ident)

    async def fetch_channel(self, ident):
        raise LookupError(ident)

    def is_ready(self):
        return True


class Message:
    def __init__(self, text, *, ident=7001, channel=None, author_id=903, bot=False):
        self.id, self.clean_content, self.channel = ident, text, channel or Channel(111)
        self.author = NS(id=author_id, display_name="Ann", bot=bot)
        self.webhook_id, self.attachments, self.embeds = None, [], []
        self.reactions, self.replies = [], []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, text, mention_author):
        self.replies.append(text)


class Interaction:
    def __init__(self, data=None, user_id=900, channel=None):
        self.data = data or {}
        self.user = NS(id=user_id, display_name="Op", name="op")
        self.channel = channel or Channel(111)
        self.sent, self.modals, self.followups, self.deferred = [], [], [], False
        outer = self

        class Response:
            async def send_message(self, text, ephemeral, allowed_mentions):
                outer.sent.append((text, ephemeral))

            async def send_modal(self, modal):
                outer.modals.append(modal)

            async def defer(self, ephemeral, thinking):
                outer.deferred = True

        class Followup:
            async def send(self, text, ephemeral, allowed_mentions):
                outer.followups.append((text, ephemeral))

        self.response, self.followup = Response(), Followup()


@pytest.fixture
async def discord_relay(authed, tmp_path):
    client, app = authed
    created = await client.post(
        "/api/v1/relays",
        json={"actor": "ann", "name": "main", "platform": "discord", "config": {"operators": ["900"], "action_users": ["900"]}},
        headers=ADMIN,
    )
    await client.post(
        "/api/v1/relays/main/bindings", json={"actor": "ann", "external_id": "111", "hub_channel": "fleet"}, headers=ADMIN
    )
    discord_client = Client()
    adapter = DiscordAdapter(fake_discord(), discord_client, MessageRefs(tmp_path / "refs.json"))
    core = RelayCore(HubClient(client, "http://testserver", created.json()["token"], "main"), adapter)
    adapter.attach(core)
    assert await core.load()
    yield adapter, core, app.state.hub, discord_client, client


# --- outbound --------------------------------------------------------------


async def test_send_builds_embed_buttons_and_safe_mentions(discord_relay):
    adapter, _core, _hub, discord_client, _client = discord_relay
    message = Rendered(
        text="fallback",
        title="Task #1",
        body="body",
        fields=(("Status", "open"),),
        footer="cat-fleet",
        buttons=(Button("Claim", "cf1:claim:task:1.1", "primary"),),
        reply_to_external="555",
    )
    sent_id = await adapter.send("111", message, nonce="cf1-2")
    sent = discord_client.channels[111].sent[-1]
    assert sent_id == "9001" and sent["content"] is None and sent["allowed_mentions"] == "NONE"
    assert sent["embed"].title == "Task #1" and sent["embed"].fields == [("Status", "open")]
    assert sent["view"].items[0].custom_id == "cf1:claim:task:1.1" and sent["view"].items[0].style == "P"
    assert sent["reference"].message_id == 555 and sent["nonce"] == "cf1-2"
    await adapter.send("111", Rendered(text="plain"))
    assert discord_client.channels[111].sent[-1]["content"] == "plain"
    with pytest.raises(ValueError):
        await adapter.send("not-a-snowflake", Rendered(text="x"))
    assert await adapter.create_thread("111", "Task #1") == "4242"
    assert adapter.status() == {"connected": True, "user": "namespace(id=1, name='bot')", "guilds": 2}


# --- inbound ---------------------------------------------------------------


async def test_on_message_relays_marks_and_remembers(discord_relay):
    adapter, _core, hub, _discord_client, _client = discord_relay
    message = Message("hello fleet")
    assert await adapter.on_message(message) == "sent"
    # ✅ is a reaction action for this relay's action users, so delivery shows the reserved 📨.
    assert message.reactions == ["📨"]
    posted = (await hub.get_messages("fleet"))["messages"][-1]
    assert posted["origin"]["relay"] == "main" and posted["author"] == "dc-Ann"
    assert adapter.refs.get(7001).hub_post_id == posted["id"]
    own = Message("echo", author_id=1)
    assert await adapter.on_message(own) == "ignored"
    unbound = Message("x", channel=Channel(222))
    assert await adapter.on_message(unbound) == "ignored" and unbound.reactions == []


async def test_thread_message_reports_parent(discord_relay):
    adapter, _core, hub, _discord_client, _client = discord_relay
    message = Message("in a thread", ident=7002, channel=Channel(333, parent_id=111))
    assert await adapter.on_message(message) == "sent"
    origin = (await hub.get_messages("fleet"))["messages"][-1]["origin"]
    assert origin["channel_id"] == "111" and origin["thread_id"] == "333"


# --- interactions ----------------------------------------------------------


async def test_reject_button_asks_for_reason_then_posts(discord_relay):
    adapter, _core, hub, _discord_client, _client = discord_relay
    hub.human_handles = frozenset({"dc-op"})
    await hub.create_proposal("builder", "demo", "Ship it", "Summary", 2)
    first = Interaction({"custom_id": "cf1:reject:proposal:P1"})
    await adapter.on_interaction(first)
    assert first.modals and first.modals[0].custom_id == "cf1:reject:proposal:P1"
    submit = Interaction(
        {
            "custom_id": "cf1:reject:proposal:P1",
            "components": [{"components": [{"custom_id": "reason", "value": "not now"}]}],
        }
    )
    reply = await adapter.on_interaction(submit)
    assert reply.ok, reply.text
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "rejected"
    stranger = Interaction({"custom_id": "cf1:reject:proposal:P1"}, user_id=902)
    await adapter.on_interaction(stranger)
    assert stranger.modals == [] and "not allowed" in stranger.sent[0][0]
    assert await adapter.on_interaction(Interaction({"custom_id": "someone-elses-button"})) is None


async def test_slash_command_defers_and_answers(discord_relay):
    adapter, _core, _hub, _discord_client, _client = discord_relay
    interaction = Interaction()
    reply = await adapter.run_command(interaction, "status", {})
    assert interaction.deferred and reply.ok
    assert interaction.followups[0][1] is True and "Relay **main**" in interaction.followups[0][0]
    public = Interaction(channel=Channel(444))
    await adapter.run_command(public, "bind", {"channel": "fleet"})
    assert public.channel.sent  # a non-private answer is also posted in the channel


def test_register_commands_builds_the_fleet_group():
    names = []

    class Group:
        def __init__(self, name, description):
            self.name = name

        def command(self, name, description):
            names.append(name)
            return lambda fn: fn

    added = []
    group = register_commands(NS(app_commands=NS(Group=Group)), NS(add_command=added.append), adapter=None)
    assert added == [group] and group.name == "fleet"
    assert set(names) == {
        "help", "status", "tasks", "proposals", "routes", "post", "bind", "unbind", "subscribe", "unsubscribe", "thread",
    }


# --- CLI -------------------------------------------------------------------


async def test_cli_seed_creates_relay_and_bindings(authed, monkeypatch):
    _client, app = authed
    real = httpx.AsyncClient

    def factory(**kwargs):
        return real(transport=httpx.ASGITransport(app=app), **kwargs)

    monkeypatch.setattr(cli.httpx, "AsyncClient", factory)
    monkeypatch.setenv("CAT_FLEET_TOKEN", "test-token")
    args = cli.build_parser().parse_args(
        ["seed", "--name", "seeded", "--map", "123=fleet,*", "--operator", "9", "--actor", "Ann B", "--url", "http://testserver"]
    )
    result = await cli.seed(args)
    assert result["token"].startswith("rly_")
    snapshot = await app.state.hub.get_relay("seeded")
    assert snapshot["bindings"][0]["external_id"] == "123" and snapshot["bindings"][0]["any_channel"] is True
    assert snapshot["relay"]["config"]["operators"] == ["9"]


def test_cli_requires_name_and_token(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--name", "Bad Name"])
    with pytest.raises(SystemExit):
        cli.main(["--name", "ok", "--token", "nope"])
    assert "rly_" in capsys.readouterr().err
