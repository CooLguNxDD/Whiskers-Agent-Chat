"""Synthetic Discord reaction actions; no discord.py import or login required."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import httpx
import pytest

from cat_fleet_chat import discord_relay as relay
from cat_fleet_chat.webhooks import chat_payloads


class Message:
    def __init__(self, text="Please review P1", *, channel=222, bot=False, webhook_id=None):
        self.id = 900
        self.clean_content = text
        self.attachments = []
        self.embeds = []
        self.channel = NS(id=channel, parent_id=None)
        self.author = NS(id=333, display_name="builder", bot=bot)
        self.webhook_id = webhook_id
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class Discord:
    def __init__(self, message):
        self.user = NS(id=999)
        self.id = message.channel.id
        self.parent_id = message.channel.parent_id
        self.message = message
        self.fetches = 0

    def get_channel(self, channel_id):
        return self

    async def fetch_message(self, message_id):
        self.fetches += 1
        return self.message

    async def fetch_user(self, user_id):
        return NS(id=user_id, display_name="alice", bot=False)


def event(emoji="✅", user=111, **kwargs):
    return NS(message_id=900, channel_id=222, user_id=user, emoji=emoji,
              member=NS(id=user, display_name="alice", bot=False), **kwargs)


@pytest.fixture
def setup(tmp_path):
    sent, votes = [], []

    async def forward(payload):
        sent.append(payload)
        return True, ""

    async def react(post_id, kind, handle):
        votes.append((post_id, kind, handle))
        return True, ""

    refs = relay.MessageRefs(tmp_path / "state.json", ref_webhooks=frozenset({55}))
    actions = relay.ReactionActions(
        {222: relay.MapEntry("fleet", frozenset({"fleet", "ops"}))},
        forward, refs, users=frozenset({111}), react=react, pace=0,
    )
    return actions, refs, sent, votes


def test_parse_defaults_and_override():
    defaults = relay.parse_reactions(None)
    assert defaults["✅"] == "approve {P}"
    assert defaults["❌"] == "reject {P} via Discord reaction"
    assert defaults["🇦"] == "{step} answer: (a)"
    assert defaults["🇪"] == "{step} answer: (e)"
    assert defaults["👍"] == "+1" and defaults["👎"] == "-1"
    assert relay.parse_reactions("✅=approve {P};❌=reject {P} via Discord reaction") == {
        "✅": "approve {P}", "❌": "reject {P} via Discord reaction"}
    assert relay.parse_reactions("") == defaults
    assert relay.parse_reaction_users(None) == frozenset()
    assert relay.parse_reaction_users(" 111, 111 ") == frozenset({111})


@pytest.mark.parametrize("raw", ["bad", "=approve {P}", "✅=", "✅=approve {oops}",
    "✅=approve {P};", "✅=approve {P};✅=pause evolve", "✅=approve {P!r}",
    "✅=do something", "✅=approve P1", "word=pause evolve",
    "✅=edit P2 title {P}", "✅=reject {P} {step}", "✅" * 20 + "=pause evolve",
    "⏸=pause evolve;⏸️=resume evolve"])
def test_malformed_map_fails(raw):
    with pytest.raises(ValueError, match="CAT_FLEET_DISCORD_REACTIONS"):
        relay.parse_reactions(raw)


@pytest.mark.parametrize("raw", ["alice", "-1", "0", "²", "111,,222"])
def test_malformed_users_fail(raw):
    with pytest.raises(ValueError, match="CAT_FLEET_DISCORD_REACTION_USERS"):
        relay.parse_reaction_users(raw)


async def test_preadd_includes_webhooks_but_not_unmapped_or_ordinary_messages(setup):
    actions, refs, _, _ = setup
    message = Message(webhook_id=55, bot=True)
    await actions.observe(message)
    assert message.reactions == ["✅", "❌"]
    assert refs.get(900).proposal == "P1"
    for message in (Message("hello"), Message("P1 and P2"), Message(channel=444)):
        await actions.observe(message)
        assert message.reactions == []
    question = Message("[S03] Which? (a) first (c) third; evolve")
    await actions.observe(question)
    assert question.reactions == ["⏸️", "▶️", "🇦", "🇨"]


@pytest.mark.parametrize(("emoji", "text", "expected"), [
    ("✅", "Review P1", "approve P1"),
    ("❌", "Review P1", "reject P1 via Discord reaction"),
    ("⏸️", "evolve is running", "pause evolve"),
    ("▶️", "evolve is paused", "resume evolve"),
    ("🇦", "[S03] Which? (a) first (b) second", "S03 answer: (a)"),
])
async def test_allowed_human_posts_command_with_origin_and_stable_id(setup, emoji, text, expected):
    actions, refs, sent, _ = setup
    message = Message(text)
    refs.remember(message, hub_post_id=42, hub_channel="ops")
    await actions.on_raw_reaction_add(event(emoji), Discord(message))
    payload = sent[0]
    assert payload["text"] == expected
    assert payload["author"] == "dc-alice" and payload["reply_to"] == 42
    assert payload["channel"] == "ops"
    assert payload["client_request_id"] == "discord-react-900-111-" + "-".join(f"{ord(c):x}" for c in emoji)
    assert payload["origin"] == {"source": "discord", "channel_id": "222", "message_id": "900", "author": "alice"}
    await actions.on_raw_reaction_add(event(emoji), Discord(message))
    assert sent[1] == payload


@pytest.mark.parametrize(("emoji", "text"), [
    ("✅", "P1 and P2"), ("✅", "no proposal"), ("🇨", "[S03] Which? (a) one"),
    ("🇦", "Which? (a) one"), ("🇦", "[S03] [S04] Which? (a) one"),
    ("⏸️", "hello"), ("🎉", "P1"),
])
async def test_ambiguous_or_inapplicable_actions_ignored(setup, emoji, text):
    actions, _, sent, votes = setup
    assert await actions.on_raw_reaction_add(event(emoji), Discord(Message(text))) == "ignored"
    assert sent == votes == []


async def test_authorisation_is_inert_and_filters_before_discord_fetch(setup):
    actions, _, sent, _ = setup
    discord = Discord(Message())
    assert await actions.on_raw_reaction_add(event(user=222), discord) == "ignored"
    actions.users = frozenset({111, 999})
    assert await actions.on_raw_reaction_add(event(user=999), discord) == "ignored"
    actions.users = frozenset()
    assert await actions.on_raw_reaction_add(event(), discord) == "ignored"
    assert discord.fetches == 0 and sent == []
    message = Message()
    await actions.observe(message)
    assert message.reactions == []


async def test_removal_does_nothing(setup):
    actions, _, sent, votes = setup
    await actions.on_raw_reaction_remove(event(), Discord(Message()))
    assert sent == votes == []


async def test_thumbs_use_feed_refs_never_chat_ids(setup):
    actions, refs, sent, votes = setup
    message = Message("A feed post")
    discord = Discord(message)
    await actions.on_raw_reaction_add(event("👍"), discord)
    refs.remember(message, hub_post_id=42)
    await actions.on_raw_reaction_add(event("👍"), discord)
    assert votes == []
    refs.remember(message, feed_post_id=73)
    await actions.observe(message)
    assert message.reactions == ["👍", "👎"]
    await actions.on_raw_reaction_add(event("👍"), discord)
    await actions.on_raw_reaction_add(event("👎"), discord)
    assert votes == [(73, "+1", "dc-alice"), (73, "-1", "dc-alice")]
    assert sent == []


def test_refs_are_bounded_atomic_and_survive_reload(tmp_path):
    path = tmp_path / "state.json"
    refs = relay.MessageRefs(path, limit=2)
    message = Message("[S03] P1? (a) yes (b) no")
    refs.remember(message, hub_post_id=42, hub_channel="ops")
    message.id = 901
    refs.remember(message, feed_post_id=73)
    reloaded = relay.MessageRefs(path, limit=2)
    assert reloaded.get(900) == refs.get(900)
    assert reloaded.get(900).hub_post_id == 42
    assert reloaded.get(900).options == ("a", "b")
    message.id = 902
    reloaded.remember(message)
    assert reloaded.get(900) is None and len(json.loads(path.read_text())) == 2
    assert len(relay.MessageRefs(path, limit=1)) == 1
    assert not list(tmp_path.glob("*.tmp"))


async def test_restart_unknown_ref_is_rebuilt_from_fetched_message(setup):
    actions, refs, sent, _ = setup
    assert refs.get(900) is None
    discord = Discord(Message("P1"))
    await actions.on_raw_reaction_add(event(), discord)
    assert sent[0]["text"] == "approve P1"
    assert "reply_to" not in sent[0]
    assert refs.get(900).proposal == "P1"


async def test_permission_error_on_preadd_is_logged_not_raised(setup, capsys):
    actions, _, _, _ = setup
    message = Message()

    async def denied(emoji):
        raise RuntimeError("Missing Permissions")

    message.add_reaction = denied
    await actions.observe(message)
    assert "Missing Permissions" in capsys.readouterr().err


async def test_hub_message_ref_captured_without_breaking_forward_api(setup):
    actions, refs, _, _ = setup

    def handler(request):
        return httpx.Response(201, json={"message": {"id": 42, "channel": "fleet"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        forward = relay.make_forward(client, "http://hub", "t", refs=refs)
        await relay.handle_message(Message(), actions.mapping, forward)
    assert refs.get(900).hub_post_id == 42
    assert refs.get(900).proposal == "P1"


async def test_feed_reaction_rest_call_uses_human_handle_and_auth():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await relay.make_feed_react(client, "http://hub/", "t")(73, "+1", "dc-alice") == (True, "")
    request = requests[0]
    assert request.method == "PUT" and request.url.path == "/api/v1/feed/posts/73/reactions/+1"
    assert request.headers["Authorization"] == "Bearer t"
    assert json.loads(request.content) == {"handle": "dc-alice"}


async def test_outbound_discord_metadata_recovers_typed_reference(setup):
    actions, refs, _, votes = setup
    message = Message("P1")
    [payload] = chat_payloads([{"kind": "feed_post", "payload": {"id": 73, "author": "builder", "body": "P1"}}], "discord", refs=True)
    message.webhook_id = 55
    message.embeds = [NS(footer=NS(text=payload["embeds"][0]["footer"]["text"]))]
    await actions.observe(message)
    assert refs.get(900).feed_post_id == 73
    await actions.on_raw_reaction_add(event("👍"), Discord(message))
    assert votes == [(73, "+1", "dc-alice")]
    # Text mentioning a feed id, or an ordinary user spoofing a footer, is not a reference.
    message.webhook_id = None
    message.id = 901
    await actions.observe(message)
    assert refs.get(901).feed_post_id is None


def test_coalesced_outbound_messages_do_not_claim_a_single_ref():
    events = [{"kind": "feed_post", "payload": {"id": n, "body": "P1"}} for n in (1, 2)]
    [payload] = chat_payloads(events, "discord", refs=True)
    assert "embeds" not in payload


def test_ref_footer_is_off_unless_the_hub_opts_in():
    event = {"kind": "feed_post", "payload": {"id": 73, "author": "builder", "body": "P1"}}
    [payload] = chat_payloads([event], "discord")
    assert "embeds" not in payload
    [payload] = chat_payloads([event], "discord", refs=True)
    assert payload["embeds"][0]["footer"]["text"].startswith("cat-fleet-ref:")


def test_hub_ref_footer_setting(monkeypatch):
    from cat_fleet_chat.config import load_settings

    monkeypatch.delenv("CAT_FLEET_DISCORD_REFS", raising=False)
    assert load_settings().discord_refs is False
    monkeypatch.setenv("CAT_FLEET_DISCORD_REFS", "1")
    assert load_settings().discord_refs is True


async def test_dispatcher_passes_ref_setting(tmp_path):
    from cat_fleet_chat.webhooks import WebhookDispatcher

    event = {"id": 5, "kind": "feed_post", "payload": {"id": 73, "author": "builder", "body": "P1"}}
    hook = {"format": "discord"}
    for enabled in (False, True):
        dispatcher = WebhookDispatcher(None, discord_refs=enabled)
        [request] = dispatcher._requests(hook, [event])
        assert ("embeds" in request.kwargs["json"]) is enabled
        await dispatcher._client.aclose()


async def test_ref_footer_only_trusted_from_the_hubs_own_webhooks(setup):
    actions, refs, _, votes = setup
    [payload] = chat_payloads(
        [{"kind": "feed_post", "payload": {"id": 73, "author": "builder", "body": "hi"}}], "discord", refs=True)
    message = Message("hi", webhook_id=66, bot=True)
    message.embeds = [NS(footer=NS(text=payload["embeds"][0]["footer"]["text"]))]
    await actions.observe(message)
    assert refs.get(900).feed_post_id is None and message.reactions == []
    await actions.on_raw_reaction_add(event("👍"), Discord(message))
    assert votes == []
    assert relay.parse_ref_webhooks(" 55,66 ") == frozenset({55, 66})
    assert relay.parse_ref_webhooks(None) == frozenset()
    with pytest.raises(ValueError, match="CAT_FLEET_DISCORD_REF_WEBHOOKS"):
        relay.parse_ref_webhooks("hook")


def test_delivery_marks_never_collide_with_actions():
    defaults = relay.parse_reactions(None)
    # Inert feature: the relay keeps its usual marks.
    assert relay.status_reactions(defaults, frozenset()) == ("✅", "❌")
    assert relay.status_reactions(defaults, frozenset({111})) == ("📨", "⚠️")
    assert relay.status_reactions({"⏸️": "pause evolve"}, frozenset({111})) == ("✅", "❌")
    assert relay.status_reactions({"✅": "approve {P}"}, frozenset({111})) == ("📨", "❌")
    for raw in ("📨=pause evolve", "⚠️=pause evolve", "⚠=resume evolve"):
        with pytest.raises(ValueError, match="CAT_FLEET_DISCORD_REACTIONS"):
            relay.parse_reactions(raw)


async def test_delivery_mark_is_not_an_action(setup):
    actions, _, sent, _ = setup
    ok, fail = relay.status_reactions(actions.reactions, actions.users)
    for mark in (ok, fail):
        assert await actions.on_raw_reaction_add(event(mark), Discord(Message())) == "ignored"
    assert sent == []

    async def forward(payload):
        return True, ""

    message = Message()
    await relay.handle_message(message, actions.mapping, forward, ok_reaction=ok, fail_reaction=fail)
    await actions.observe(message)
    assert message.reactions == ["📨", "✅", "❌"]


async def test_state_writes_run_off_the_event_loop_and_coalesce(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    refs = relay.MessageRefs(path)
    threads = []
    real = relay.asyncio.to_thread

    async def to_thread(func, *args):
        threads.append(func)
        return await real(func, *args)

    monkeypatch.setattr(relay.asyncio, "to_thread", to_thread)
    for message_id in (900, 901, 902):
        message = Message("P1")
        message.id = message_id
        refs.remember(message)
    assert not path.exists()  # nothing written synchronously on the loop
    await refs.flush()
    assert threads == [refs._write]
    assert len(relay.MessageRefs(path)) == 3


async def test_webhook_chat_metadata_restores_reply_channel(setup):
    actions, refs, sent, _ = setup
    [body] = chat_payloads([{
        "kind": "message.created", "channel": "ops", "entity_id": 42,
        "payload": {"id": 42, "text": "P1", "author": "builder"},
    }], "discord", refs=True)
    message = Message(webhook_id=55, bot=True)
    message.embeds = [NS(footer=NS(text=body["embeds"][0]["footer"]["text"]))]
    await actions.on_raw_reaction_add(event(), Discord(message))
    assert sent[0]["reply_to"] == 42 and sent[0]["channel"] == "ops"
    assert refs.get(900).feed_post_id is None


async def test_unknown_thread_channel_and_missing_member_are_fetched(setup):
    actions, _, sent, _ = setup
    message = Message(channel=444)
    message.channel.parent_id = 222
    discord = Discord(message)
    discord.get_channel = lambda channel_id: None

    async def fetch_channel(channel_id):
        return discord

    discord.fetch_channel = fetch_channel
    payload = event()
    payload.channel_id = 444
    payload.member = None
    await actions.on_raw_reaction_add(payload, discord)
    assert sent[0]["author"] == "dc-alice"
    assert sent[0]["origin"]["channel_id"] == "222"


async def test_discord_fetch_error_and_allowlisted_bot_are_ignored(setup, capsys):
    actions, _, sent, _ = setup
    discord = Discord(Message())
    payload = event()
    payload.member.bot = True
    assert await actions.on_raw_reaction_add(payload, discord) == "ignored"

    async def denied(message_id):
        raise RuntimeError("Unknown Message")

    discord.fetch_message = denied
    assert await actions.on_raw_reaction_add(event(), discord) == "ignored"
    assert "Unknown Message" in capsys.readouterr().err
    assert sent == []


async def test_fetched_edit_rechecks_proposal_and_map_allowlist(setup):
    actions, refs, sent, _ = setup
    message = Message()
    refs.remember(message, hub_post_id=42, hub_channel="ops")
    message.clean_content = "P1 and P2"
    assert await actions.on_raw_reaction_add(event(), Discord(message)) == "ignored"
    message.clean_content = "P1"
    actions.mapping = {222: relay.MapEntry("fleet", frozenset({"fleet"}))}
    assert await actions.on_raw_reaction_add(event(), Discord(message)) == "ignored"
    assert sent == []


async def test_preadds_are_paced_and_bounded(setup, monkeypatch):
    actions, _, _, _ = setup
    pauses = []

    async def sleep(delay):
        pauses.append(delay)

    monkeypatch.setattr(relay.asyncio, "sleep", sleep)
    actions.pace = 0.25
    message = Message("P1 evolve [S03] Which? (a) a (b) b (c) c (d) d (e) e")
    await actions.observe(message)
    assert len(message.reactions) == 9
    assert pauses == [0.25] * 9


@pytest.mark.parametrize("raw", ["broken", "{}", '[[900, 1]]', '[[900, {"hub_post_id": "oops"}]]'])
def test_corrupt_state_is_logged_and_rebuilt(tmp_path, capsys, raw):
    path = tmp_path / "state.json"
    path.write_text(raw)
    refs = relay.MessageRefs(path)
    assert len(refs) == 0
    assert "could not load" in capsys.readouterr().err
    refs.remember(Message())
    assert relay.MessageRefs(path).get(900).proposal == "P1"


async def test_state_write_error_does_not_prevent_command(setup, monkeypatch, capsys):
    actions, _, sent, _ = setup

    def denied(*args):
        raise OSError("read-only state directory")

    monkeypatch.setattr(relay.os, "replace", denied)
    assert await actions.on_raw_reaction_add(event(), Discord(Message())) == "sent"
    assert sent[0]["text"] == "approve P1"
    await actions.refs.flush()
    assert "could not save" in capsys.readouterr().err


async def test_feed_put_retries_transient_failures_but_not_4xx(monkeypatch):
    monkeypatch.setattr(relay, "RETRY_DELAY", 0)
    statuses = [500, 201, 403]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(statuses.pop(0), json={"error": {"message": "forbidden"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        react = relay.make_feed_react(client, "http://hub", "t")
        assert await react(73, "+1", "dc-alice") == (True, "")
        assert await react(73, "+1", "dc-alice") == (False, "forbidden")
    assert len(calls) == 3


def test_main_wires_raw_handlers_intents_and_environment(tmp_path, monkeypatch):
    """Exercise the actual nested Discord Client without importing discord.py."""
    import asyncio
    import sys

    captured = {}

    class Client:
        def __init__(self, *, intents):
            captured["intents"] = intents
            captured["client"] = self

        def run(self, token):
            assert token == "synthetic"

            async def check():
                await self.setup_hook()
                assert self.actions.users == frozenset({111})
                assert self.actions.reactions == {"✅": "approve {P}"}
                assert self.actions.refs.path == tmp_path / "state.json"
                assert callable(self.on_raw_reaction_add)
                order = []

                async def forward(payload):
                    order.append("forward")
                    return True, ""

                async def observe(message):
                    order.append("observe")

                self.forward = forward
                self.actions.observe = observe
                message = Message()
                await self.on_message(message)
                assert order == ["forward", "observe"]
                # ✅ is an action here, so delivery shows the reserved mark.
                assert message.reactions == ["📨"]
                await self.on_raw_reaction_remove(event())
                await self.close()

            asyncio.run(check())

        async def close(self):
            pass

    monkeypatch.setitem(sys.modules, "discord", NS(Client=Client, Intents=NS(default=lambda: NS())))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("CAT_FLEET_DISCORD_REACTION_USERS", "111")
    monkeypatch.setenv("CAT_FLEET_DISCORD_REACTIONS", "✅=approve {P}")
    monkeypatch.setenv("CAT_FLEET_DISCORD_STATE", str(tmp_path / "state.json"))
    relay.main(["--map", "222=fleet"])
    assert captured["intents"].message_content and captured["intents"].reactions
    assert captured["client"].http_client.is_closed


def test_invalid_environment_fails_at_startup(monkeypatch, capsys):
    monkeypatch.setenv("CAT_FLEET_DISCORD_REACTIONS", "malformed")
    with pytest.raises(SystemExit) as error:
        relay.main(["--map", "222=fleet"])
    assert error.value.code == 2
    assert "CAT_FLEET_DISCORD_REACTIONS" in capsys.readouterr().err


@pytest.mark.parametrize("allowed", [True, False])
async def test_reaction_command_reaches_real_hub_allowlist_and_is_idempotent(authed, tmp_path, allowed):
    client, app = authed
    app.state.hub.human_handles = frozenset({"dc-alice"}) if allowed else frozenset()
    proposal = (await app.state.hub.create_proposal(
        "builder", "demo", "Test proposal", "Synthetic proposal", 3,
    ))["proposal"]
    assert proposal["id"] == "P1"
    message = Message("P1")
    refs = relay.MessageRefs(tmp_path / "state.json")
    forward = relay.make_forward(client, "http://testserver", "test-token", refs=refs)
    actions = relay.ReactionActions(
        {222: relay.MapEntry("fleet", frozenset({"fleet"}))}, forward, refs,
        users=frozenset({111}), pace=0,
    )
    await relay.handle_message(message, actions.mapping, forward)
    parent_id = refs.get(900).hub_post_id
    for _ in range(2):
        assert await actions.on_raw_reaction_add(event(), Discord(message)) == "sent"
    found = await app.state.hub.get_proposal("P1")
    assert found["proposal"]["status"] == ("accepted" if allowed else "open")
    posts = (await app.state.hub.get_messages("fleet"))["messages"]
    commands = [post for post in posts if post["text"] == "approve P1"]
    assert len(commands) == 1 and commands[0]["reply_to"] == parent_id
    assert len([post for post in posts if post["author"] == "evolve"]) == (1 if allowed else 0)
