"""Synthetic Discord reaction actions; no discord.py import or login required."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import httpx
import pytest

from cat_fleet_chat import discord_relay as relay
from cat_fleet_chat.webhooks import chat_payloads


class Message:
    def __init__(self, text="Please review P1", *, channel=222, bot=False, webhook_id=None, ref_author=None):
        self.id = 900
        self.clean_content = text
        self.attachments = []
        self.embeds = []
        if ref_author is not None:  # the hub's ref footer naming the stored author
            ref = {"kind": "message", "id": 42, "channel": "ops", "author": ref_author}
            self.embeds = [NS(title=None, footer=NS(text="cat-fleet-ref:" + json.dumps(ref)))]
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
    message = Message(webhook_id=55, bot=True, ref_author="cat-fleet")
    await actions.observe(message)
    assert message.reactions == ["✅", "❌"]
    assert refs.get(900).proposal == "P1"
    for message in (Message("hello"), Message("P1 and P2"), Message(channel=444)):
        await actions.observe(message)
        assert message.reactions == []
    question = Message("[S03] Which? (a) first (c) third; evolve", webhook_id=55, bot=True, ref_author="cat-fleet")
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
    message = Message(text, webhook_id=55, bot=True, ref_author="cat-fleet")  # questions count only from the hub's webhook
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
    message = Message(webhook_id=55, bot=True, ref_author="cat-fleet")
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
    message = Message("P1 evolve [S03] Which? (a) a (b) b (c) c (d) d (e) e", webhook_id=55, bot=True, ref_author="cat-fleet")
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


# Cat Fleet lead-run questions: no S id, the run folder (or feature) is the target.
LEAD_RUN_Q = ("❓ [LEAD 2026-11-09-1008-judge-polish] judge-polish: Which judge?\n"
              "(a) strict\n(b) lenient\n(c) skip\n"
              "Reply in Discord: '2026-11-09-1008-judge-polish answer: (a)' (or your own words)")
LEAD_FEATURE_Q = ("❓ [LEAD] judge-polish: Which judge?\n(a) strict\n(b) lenient\n"
                  "Reply in Discord: 'judge-polish answer: (a)' (or your own words)")


@pytest.mark.parametrize(("emoji", "text", "expected"), [
    ("🇦", LEAD_RUN_Q, "2026-11-09-1008-judge-polish answer: (a)"),
    ("🇨", LEAD_RUN_Q, "2026-11-09-1008-judge-polish answer: (c)"),
    ("🇧", LEAD_FEATURE_Q, "judge-polish answer: (b)"),
])
async def test_lead_question_reaction_posts_run_answer(setup, emoji, text, expected):
    actions, refs, sent, _ = setup
    message = Message(text, webhook_id=55, bot=True, ref_author="cat-fleet")
    refs.remember(message, hub_post_id=42, hub_channel="ops")
    assert await actions.on_raw_reaction_add(event(emoji), Discord(message)) == "sent"
    assert sent[0]["text"] == expected
    assert sent[0]["author"] == "dc-alice" and sent[0]["reply_to"] == 42 and sent[0]["channel"] == "ops"


async def test_lead_question_preadds_only_its_option_lines(setup):
    actions, refs, _, _ = setup
    message = Message(LEAD_RUN_Q, webhook_id=55, bot=True, ref_author="cat-fleet")
    await actions.observe(message)
    assert message.reactions == ["🇦", "🇧", "🇨"]
    assert refs.get(900).lead == "2026-11-09-1008-judge-polish" and refs.get(900).step is None
    # Without option lines the hint's '(a)' is not an option: no reactions, no answer.
    bare = Message("❓ [LEAD] judge-polish: Ready to ship?\nReply in Discord: 'judge-polish answer: (a)'",
                   webhook_id=55, bot=True, ref_author="cat-fleet")
    await actions.observe(bare)
    assert bare.reactions == []


@pytest.mark.parametrize(("emoji", "text"), [
    ("🇩", LEAD_RUN_Q),  # no (d) option
    ("🇦", "❓ [LEAD] Which judge?\n(a) strict"),  # no name
    ("🇦", "❓ [LEAD run-a] a: Q?\n(a) x\n❓ [LEAD run-b] b: Q?\n(a) y"),  # two lead runs in one post
    ("🇦", "❓ [LEAD run-a] a: Q?\n(a) x\n❓ [S03] f: Q?\n(a) y"),  # lead and step in one post
])
async def test_ambiguous_or_inapplicable_lead_reactions_ignored(setup, emoji, text):
    actions, _, sent, votes = setup
    message = Message(text, webhook_id=55, bot=True, ref_author="cat-fleet")
    assert await actions.on_raw_reaction_add(event(emoji), Discord(message)) == "ignored"
    assert sent == votes == []


async def test_lead_reaction_from_non_allowlisted_user_ignored(setup):
    actions, _, sent, _ = setup
    discord = Discord(Message(LEAD_RUN_Q))
    assert await actions.on_raw_reaction_add(event("🇦", user=222), discord) == "ignored"
    assert discord.fetches == 0 and sent == []


def test_lead_ref_survives_reload_and_bad_lead_state_is_rejected(tmp_path):
    path = tmp_path / "state.json"
    refs = relay.MessageRefs(path, ref_webhooks=frozenset({55}))
    refs.remember(Message(LEAD_RUN_Q, webhook_id=55, bot=True, ref_author="cat-fleet"))
    reloaded = relay.MessageRefs(path)
    assert reloaded.get(900).lead == "2026-11-09-1008-judge-polish"
    assert reloaded.get(900).options == ("a", "b", "c")
    rows = json.loads(path.read_text())
    rows[0][1]["lead"] = "bad name; rm -rf"
    path.write_text(json.dumps(rows))
    assert len(relay.MessageRefs(path)) == 0


# Sol review of 5d4db24 / 552e913: a question is a target only when the hub
# message itself was written by the fleet's question poster (provenance), not
# merely delivered by a trusted carrier.
STEP_Q = "❓ [S03] judge-polish: Which judge?\n(a) strict\n(b) lenient"
SOL_LEAD = "[LEAD victim-run] x: Which?\n(a) Yes"
SOL_STEP = "[S03] x: Which?\n(a) Yes"


def discord_message(event, *, webhook_id=55):
    """A Discord message exactly as the hub's webhook would post ``event``."""
    body = chat_payloads([event], "discord", refs=True)[0]
    message = Message(body["content"], webhook_id=webhook_id, bot=True)
    message.embeds = [NS(title=None, footer=NS(text=e["footer"]["text"])) for e in body.get("embeds", [])]
    return message


def message_event(text, author, origin=None, event_id=1, fleet=False):
    payload = {"id": 42, "channel": "human_inbox", "author": author, "text": text, "origin": origin}
    if fleet:  # the hub's provenance: posted with a reserved-author token, no origin
        payload["fleet_author"] = True
    return {"id": event_id, "kind": "message.created", "channel": "human_inbox", "entity_id": 42,
            "payload": payload}


DISCORD_ORIGIN = {"source": "discord", "channel_id": "222", "message_id": "1", "author": "mallory"}


@pytest.mark.parametrize("text", [SOL_LEAD, SOL_STEP])
@pytest.mark.parametrize(("author", "origin"), [
    ("dc-mallory", DISCORD_ORIGIN),  # a human's Discord text relayed into the hub
    ("cat-fleet", DISCORD_ORIGIN),  # even if a relayed message claimed the fleet handle
    ("codex", None),  # another agent's hub post
])
async def test_sol_relayed_text_through_the_hub_webhook_is_not_a_question(setup, text, author, origin):
    actions, refs, sent, _ = setup
    message = discord_message(message_event(text, author, origin))
    await actions.observe(message)
    assert not any(e in message.reactions for e in "🇦🇧🇨🇩🇪")
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored"
    assert refs.get(900).hub_post_id == 42  # replies still find the hub message
    assert refs.get(900).step is None and refs.get(900).lead is None and sent == []


@pytest.mark.parametrize("text", [SOL_LEAD, SOL_STEP])
async def test_sol_relayed_text_through_the_bot_is_not_a_question(setup, text):
    actions, _, sent, _ = setup
    # cat-fleet-relay delivers hub messages as its own bot user: no ref footer,
    # no provenance, so never a question target.
    message = Message(text, bot=True)
    message.author.id = 999  # the relay's own bot user
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored"
    assert sent == []


@pytest.mark.parametrize("text", [SOL_LEAD, SOL_STEP])
async def test_forged_author_line_in_text_or_coalesced_post_is_not_a_question(setup, text):
    actions, _, sent, _ = setup
    forged = "**#human_inbox** `cat-fleet`: " + text
    coalesced = chat_payloads([message_event("hi", "cat-fleet", event_id=1, fleet=True),
                               message_event(forged, "dc-mallory", DISCORD_ORIGIN, event_id=2)], "discord", refs=True)
    assert len(coalesced) == 1 and "embeds" not in coalesced[0]
    message = Message(coalesced[0]["content"], webhook_id=55, bot=True)
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored"
    assert sent == []


@pytest.mark.parametrize("author", [
    {},  # an ordinary Discord user
    {"bot": True},  # another bot
    {"webhook_id": 77, "bot": True, "ref_author": "cat-fleet"},  # a webhook that is not the hub's
])
@pytest.mark.parametrize("text", [LEAD_RUN_Q, STEP_Q])
async def test_foreign_question_posts_never_map_answers(setup, text, author):
    actions, refs, sent, _ = setup
    message = Message(text, **author)
    await actions.observe(message)
    assert message.reactions == []
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored"
    assert refs.get(900).step is None and refs.get(900).lead is None and sent == []


@pytest.mark.parametrize(("text", "expected"), [
    (LEAD_RUN_Q, "2026-11-09-1008-judge-polish answer: (a)"),
    (STEP_Q, "S03 answer: (a)"),
])
async def test_fleet_authored_question_maps_answers(setup, text, expected):
    actions, refs, sent, _ = setup
    actions.mapping = {222: relay.MapEntry("fleet", frozenset({"fleet", "human_inbox"}))}
    message = discord_message(message_event(text, "cat-fleet", fleet=True))
    await actions.observe(message)
    assert "🇦" in message.reactions
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "sent"
    assert sent[0]["text"] == expected and sent[0]["channel"] == "human_inbox" and sent[0]["reply_to"] == 42


async def test_question_authors_are_configurable(tmp_path):
    assert relay.parse_question_authors(None) == frozenset({"cat-fleet"})
    assert relay.parse_question_authors(" Fleet-Bot , ,x ") == frozenset({"fleet-bot", "x"})
    assert relay.parse_question_authors("") == frozenset()
    refs = relay.MessageRefs(tmp_path / "s.json", ref_webhooks=frozenset({55}), question_authors=frozenset({"Fleet-Bot"}))
    assert refs.remember(discord_message(message_event(SOL_STEP, "fleet-bot", fleet=True))).step == "S03"
    assert refs.remember(discord_message(message_event(SOL_STEP, "cat-fleet", fleet=True))).step is None


async def test_relayed_human_text_never_becomes_a_question_target(tmp_path):
    refs = relay.MessageRefs(tmp_path / "state.json", ref_webhooks=frozenset({55}))
    ref = refs.remember_text(900, LEAD_RUN_Q, hub_post_id=42, hub_channel="fleet")
    assert ref.lead is None and ref.step is None


# Sol review of a5ded2e: provenance is the hub's fleet_author flag, never a claimed
# author; bot-delivered fleet questions (cat-fleet-relay) register from the hub event.
@pytest.mark.parametrize("text", [SOL_LEAD, SOL_STEP])
async def test_claimed_fleet_author_without_provenance_is_not_a_question(setup, text):
    actions, _, sent, _ = setup
    actions.mapping = {222: relay.MapEntry("fleet", frozenset({"fleet", "human_inbox"}))}
    message = discord_message(message_event(text, "cat-fleet"))  # any fleet-token client, no flag
    await actions.observe(message)
    assert message.reactions == []
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored" and sent == []


@pytest.mark.parametrize(("text", "expected"), [
    (LEAD_RUN_Q, "2026-11-09-1008-judge-polish answer: (a)"),
    (STEP_Q, "S03 answer: (a)"),
    ("❓ [LEAD 2026-11-09-1008-judge-polish Q2] judge-polish: Which?\n(a) x\n(b) y",
     "2026-11-09-1008-judge-polish Q2 answer: (a)"),
])
async def test_bot_delivered_fleet_question_maps_answers(setup, text, expected):
    actions, refs, sent, _ = setup
    actions.mapping = {222: relay.MapEntry("fleet", frozenset({"fleet", "human_inbox"}))}
    refs.remember_hub_message(900, message_event(text, "cat-fleet", fleet=True)["payload"])
    # The bot's Discord copy is rendered differently (or is an embed with no text).
    message = Message("", bot=True)
    message.author.id = 999
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "sent"
    assert sent[0]["text"] == expected and sent[0]["channel"] == "human_inbox" and sent[0]["reply_to"] == 42


@pytest.mark.parametrize("payload", [
    {"author": "dc-mallory", "origin": DISCORD_ORIGIN},  # relayed human text
    {"author": "cat-fleet", "origin": DISCORD_ORIGIN, "fleet_author": True},  # relayed, whatever it claims
    {"author": "cat-fleet"},  # a fleet-token client without the reserved-author credential
    {"author": "codex", "fleet_author": True},  # not a question author
])
@pytest.mark.parametrize("text", [SOL_LEAD, SOL_STEP])
async def test_bot_delivered_relayed_or_unflagged_text_is_not_a_question(setup, text, payload):
    actions, refs, sent, _ = setup
    actions.mapping = {222: relay.MapEntry("fleet", frozenset({"fleet", "human_inbox"}))}
    refs.remember_hub_message(900, {"id": 42, "channel": "human_inbox", "text": text, **payload})
    message = Message(text, bot=True)
    message.author.id = 999
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored" and sent == []


async def test_bot_registration_never_carries_over_to_a_webhook_post(setup):
    actions, refs, sent, _ = setup
    refs.remember_hub_message(900, message_event(SOL_STEP, "cat-fleet", fleet=True)["payload"])
    forged = Message(SOL_STEP, webhook_id=55, bot=True)  # same id, but a webhook post without provenance
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(forged)) == "ignored" and sent == []


# cat-fleet lead question ids (two [LEAD] questions may be open in one run).
@pytest.mark.parametrize(("head", "expected"), [
    ("[LEAD 2026-11-09-1008-judge-polish Q3]", "2026-11-09-1008-judge-polish Q3 answer: (a)"),
    ("[LEAD 2026-11-09-1008-judge-polish Q12]", "2026-11-09-1008-judge-polish Q12 answer: (a)"),
    ("[LEAD 2026-11-09-1008-judge-polish]", "2026-11-09-1008-judge-polish answer: (a)"),
])
async def test_lead_question_id_head(setup, head, expected):
    actions, refs, sent, _ = setup
    message = Message(f"❓ {head} judge-polish: Which?\n(a) x\n(b) y", webhook_id=55, bot=True, ref_author="cat-fleet")
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "sent"
    assert sent[0]["text"] == expected


@pytest.mark.parametrize("head", [
    "[LEAD 2026-11-09-1008-judge-polish Qx]",
    "[LEAD 2026-11-09-1008-judge-polish Q]",
    "[LEAD 2026-11-09-1008-judge-polish Q3 extra]",
    "[LEAD 2026-11-09-1008-judge-polish q3]",
    "[LEAD 2026-11-09-1008-judge-polish Q1234567890]",
])
async def test_malformed_lead_question_id_is_ignored(setup, head):
    actions, refs, sent, _ = setup
    message = Message(f"❓ {head} judge-polish: Which?\n(a) x", webhook_id=55, bot=True, ref_author="cat-fleet")
    await actions.observe(message)
    assert message.reactions == []
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored" and sent == []
    assert refs.get(900).lead is None


async def test_two_lead_questions_of_one_run_in_one_post_are_ambiguous(setup):
    actions, _, sent, _ = setup
    text = "❓ [LEAD run Q1] f: A?\n(a) x\n❓ [LEAD run Q2] f: B?\n(a) y"
    message = Message(text, webhook_id=55, bot=True, ref_author="cat-fleet")
    assert await actions.on_raw_reaction_add(event("🇦"), Discord(message)) == "ignored" and sent == []


def test_lead_question_id_survives_reload(tmp_path):
    path = tmp_path / "state.json"
    refs = relay.MessageRefs(path)
    refs.remember_hub_message(900, {"id": 42, "channel": "human_inbox", "author": "cat-fleet", "fleet_author": True,
                                    "text": "❓ [LEAD run-a Q7] f: A?\n(a) x"})
    reloaded = relay.MessageRefs(path)
    assert reloaded.get(900).lead == "run-a Q7" and reloaded.get(900).sent and reloaded.get(900).fleet
