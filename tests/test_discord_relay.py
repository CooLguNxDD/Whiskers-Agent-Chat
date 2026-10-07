"""The Discord relay: pure helpers, then a fake Discord message against a real hub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
import pytest

from cat_fleet_chat import discord_relay
from cat_fleet_chat.discord_relay import (
    FAIL_REACTION,
    OK_REACTION,
    MapEntry,
    author_handle,
    build_post,
    handle_message,
    make_channels,
    make_forward,
    parse_map,
    parse_target,
)
from cat_fleet_chat.validate import HANDLE_RE

HUB = "http://testserver"


@dataclass
class FakeAttachment:
    url: str


@dataclass
class FakeAuthor:
    display_name: str = "Andrew"
    bot: bool = False


@dataclass
class FakeChannel:
    id: int = 111
    parent_id: int | None = None


@dataclass
class FakeMessage:
    id: int = 900
    content: str = "hello fleet"
    author: FakeAuthor = field(default_factory=FakeAuthor)
    channel: FakeChannel = field(default_factory=FakeChannel)
    webhook_id: int | None = None
    attachments: list[FakeAttachment] = field(default_factory=list)
    reactions: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    @property
    def clean_content(self) -> str:
        return self.content

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def reply(self, text: str, mention_author: bool = True) -> None:
        self.replies.append(text)


def entry(default="fleet", *more, any_channel=False):
    return MapEntry(default, frozenset([default, *more]), any_channel)


MAPPING = {111: entry()}


async def never(_payload):  # a forward that must not be reached
    raise AssertionError("forward should not be called")


# ---- pure --------------------------------------------------------------


def test_parse_map():
    assert parse_map(["111=fleet", " 222 = ops "]) == {111: entry("fleet"), 222: entry("ops")}
    for bad in (["fleet"], ["abc=fleet"], ["111="], ["111=Bad Name"], ["111=fleet,Bad Name"], ["111=*"], []):
        with pytest.raises(ValueError):
            parse_map(bad)


def test_author_handle_is_always_a_valid_prefixed_handle():
    assert author_handle("Andrew L.") == "dc-Andrew-L"
    assert author_handle("猫猫") == "dc-user"
    assert author_handle("a" * 200).startswith("dc-") and len(author_handle("a" * 200)) <= 64
    assert author_handle("x", prefix="chat-") == "chat-x"
    for name in ("Andrew L.", "猫猫", "a" * 200, "--", "  ", "o'brien", "A_B-C"):
        assert HANDLE_RE.fullmatch(author_handle(name)), name


def test_build_post_keeps_attachments_and_a_stable_request_id():
    message = FakeMessage(id=42, attachments=[FakeAttachment("https://cdn.test/a.png")])
    assert build_post(message, "fleet") == {
        "channel": "fleet",
        "author": "dc-Andrew",
        "text": "hello fleet\nhttps://cdn.test/a.png",
        "client_request_id": "discord-42",
        "origin": {"source": "discord", "channel_id": "111", "message_id": "42", "author": "Andrew"},
    }
    assert build_post(FakeMessage(content="", attachments=[FakeAttachment("u")]), "fleet")["text"] == "u"


# ---- handle_message ----------------------------------------------------


async def test_ignores_bots_webhooks_unmapped_channels_and_empty_messages():
    for message in (
        FakeMessage(author=FakeAuthor(bot=True)),
        FakeMessage(webhook_id=5),  # the hub's own outbound post
        FakeMessage(channel=FakeChannel(id=999)),
        FakeMessage(content="  "),
    ):
        assert await handle_message(message, MAPPING, never) == "ignored"
        assert message.reactions == [] and message.replies == []


async def test_threads_use_the_parent_channel_mapping():
    sent = []

    async def forward(payload):
        sent.append(payload)
        return True, ""

    message = FakeMessage(channel=FakeChannel(id=777, parent_id=111))
    assert await handle_message(message, MAPPING, forward) == "sent"
    assert sent[0]["channel"] == "fleet"


async def test_success_reacts_and_failure_replies_with_the_reason():
    async def good(_payload):
        return True, ""

    async def bad(_payload):
        return False, "channel old is archived"

    ok = FakeMessage()
    assert await handle_message(ok, MAPPING, good) == "sent"
    assert ok.reactions == [OK_REACTION] and ok.replies == []
    failed = FakeMessage()
    assert await handle_message(failed, MAPPING, bad) == "failed"
    assert failed.reactions == [FAIL_REACTION]
    assert "archived" in failed.replies[0]


async def test_a_discord_permission_error_does_not_crash_the_relay():
    class Muted(FakeMessage):
        async def add_reaction(self, emoji: str) -> None:
            raise RuntimeError("Missing Permissions")

    async def good(_payload):
        return True, ""

    assert await handle_message(Muted(), MAPPING, good) == "sent"


# ---- forward -----------------------------------------------------------


async def test_forward_retries_5xx_but_not_4xx(monkeypatch):
    monkeypatch.setattr(discord_relay, "RETRY_DELAY", 0)
    calls = []

    def flaky(request):
        calls.append(request)
        return httpx.Response(500 if len(calls) < 3 else 201, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        assert await make_forward(client, HUB, "t")({"text": "x"}) == (True, "")
    assert len(calls) == 3

    calls.clear()

    def refuse(request):
        calls.append(request)
        return httpx.Response(409, json={"error": {"code": "channel_archived", "message": "archived"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        assert await make_forward(client, HUB, "t")({"text": "x"}) == (False, "archived")
    assert len(calls) == 1


async def test_forward_gives_up_when_the_hub_is_unreachable(monkeypatch):
    monkeypatch.setattr(discord_relay, "RETRY_DELAY", 0)
    calls = []

    def down(request):
        calls.append(request)
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        ok, reason = await make_forward(client, HUB, "t")({"text": "x"})
    assert ok is False and "unreachable" in reason
    assert len(calls) == discord_relay.ATTEMPTS


# ---- against a real hub ------------------------------------------------


async def test_relayed_message_lands_in_the_hub_and_replays_are_idempotent(authed):
    client, _app = authed
    forward = make_forward(client, HUB, "test-token")
    message = FakeMessage(id=123, content="@builder please look at this")
    assert await handle_message(message, MAPPING, forward) == "sent"
    assert await handle_message(FakeMessage(id=123, content="@builder please look at this"), MAPPING, forward) == "sent"
    messages = (
        await client.get("/api/v1/messages", params={"channel": "fleet"}, headers={"Authorization": "Bearer test-token"})
    ).json()["messages"]
    assert len(messages) == 1
    assert (messages[0]["author"], messages[0]["mentions"]) == ("dc-Andrew", ["builder"])
    assert message.reactions == [OK_REACTION]


async def test_relaying_into_an_archived_channel_tells_the_sender(authed):
    client, _app = authed
    auth = {"Authorization": "Bearer test-token"}
    await client.post("/api/v1/channels", json={"name": "old"}, headers=auth)
    await client.post("/api/v1/channels/old/archive", json={"actor": "claude"}, headers=auth)
    message = FakeMessage()
    result = await handle_message(message, {111: entry("old")}, make_forward(client, HUB, "test-token"))
    assert result == "failed"
    assert message.reactions == [FAIL_REACTION]
    assert "archived" in message.replies[0]


async def test_exclude_authors_dc_star_stops_the_echo_but_not_agent_replies(hooked):
    client, _app, sink = hooked
    created = await client.post(
        "/api/v1/webhooks",
        json={
            "name": "discord",
            "direction": "out",
            "url": "https://discord.test/api/webhooks/1/TOKEN",
            "format": "discord",
            "exclude_authors": ["dc-*"],
        },
    )
    assert created.status_code == 201
    forward = make_forward(client, HUB, "test-token")
    assert await handle_message(FakeMessage(content="from a human in discord"), MAPPING, forward) == "sent"
    await client.post(
        "/api/v1/messages", json={"channel": "fleet", "author": "builder", "text": "agent reply"}
    )
    for _ in range(100):
        if sink.requests:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.25)
    text = "\n".join(body["content"] for body in sink.bodies())
    assert "agent reply" in text
    assert "from a human in discord" not in text


# ---- routing: allow-list, #token targeting, origin ---------------------

KNOWN = frozenset({"fleet", "ops", "review", "secret"})


def channels_of(names):
    async def channels():
        return None if names is None else set(names)

    return channels


def recorder():
    sent = []

    async def forward(payload):
        sent.append(payload)
        return True, ""

    return sent, forward


def test_parse_map_allow_list_and_wildcard():
    parsed = parse_map(["111=fleet,ops,review", "222=fleet,*"])
    assert parsed[111] == entry("fleet", "ops", "review")
    assert parsed[222].default == "fleet" and parsed[222].any_channel is True


def test_parse_target_picks_an_allowed_hub_channel_and_strips_the_token():
    allowed = entry("fleet", "ops")
    assert parse_target("@builder #ops run the tests", allowed, KNOWN) == (
        "ops",
        "@builder run the tests",
        None,
    )
    assert parse_target("run the tests #ops please", allowed, KNOWN) == (
        "ops",
        "run the tests please",
        None,
    )
    assert parse_target("@builder hi", allowed, KNOWN) == ("fleet", "@builder hi", None)


def test_parse_target_leaves_unknown_tokens_and_ignores_lookalikes():
    allowed = entry("fleet", "ops")
    # Not a hub channel: probably a Discord channel mention, so it stays in the text.
    assert parse_target("see #general", allowed, KNOWN) == ("fleet", "see #general", None)
    for text in ("issue#ops", "https://x.test/a#ops", "c#ops", "##ops"):
        assert parse_target(text, allowed, KNOWN) == ("fleet", text, None)


def test_parse_target_refuses_channels_the_map_does_not_allow():
    channel, text, note = parse_target("@builder #secret do it", entry("fleet", "ops"), KNOWN)
    assert (channel, text) == ("fleet", "@builder #secret do it")
    assert "#secret" in note
    assert parse_target("#secret hi", entry("fleet", any_channel=True), KNOWN)[0] == "secret"


def test_parse_target_does_nothing_without_a_channel_list():
    assert parse_target("#ops hi", entry("fleet", "ops"), None) == ("fleet", "#ops hi", None)


async def test_a_hub_channel_token_retargets_the_message_and_keeps_its_origin():
    sent, forward = recorder()
    message = FakeMessage(id=7, content="@builder #ops run the tests")
    mapping = {111: entry("fleet", "ops")}
    assert await handle_message(message, mapping, forward, channels=channels_of(KNOWN)) == "sent"
    assert (sent[0]["channel"], sent[0]["text"]) == ("ops", "@builder run the tests")
    assert sent[0]["origin"]["channel_id"] == "111" and sent[0]["origin"]["message_id"] == "7"
    assert message.reactions == [OK_REACTION] and message.replies == []


async def test_a_channel_the_map_forbids_goes_to_the_default_with_a_note():
    sent, forward = recorder()
    message = FakeMessage(content="#secret do it")
    mapping = {111: entry("fleet", "ops")}
    assert await handle_message(message, mapping, forward, channels=channels_of(KNOWN)) == "sent"
    assert (sent[0]["channel"], sent[0]["text"]) == ("fleet", "#secret do it")
    assert message.reactions == [OK_REACTION] and "#secret" in message.replies[0]


async def test_without_a_channel_list_everything_goes_to_the_default():
    sent, forward = recorder()
    mapping = {111: entry("fleet", "ops")}
    await handle_message(FakeMessage(content="#ops hi"), mapping, forward, channels=channels_of(None))
    assert (sent[0]["channel"], sent[0]["text"]) == ("fleet", "#ops hi")


async def test_the_channel_list_is_only_fetched_when_a_token_is_present():
    sent, forward = recorder()
    calls = []

    async def channels():
        calls.append(1)
        return set(KNOWN)

    mapping = {111: entry("fleet", "ops")}
    await handle_message(FakeMessage(content="no token here"), mapping, forward, channels=channels)
    assert calls == []
    await handle_message(FakeMessage(id=2, content="#ops now"), mapping, forward, channels=channels)
    assert calls == [1]


async def test_a_thread_reply_points_back_at_the_parent_channel():
    sent, forward = recorder()
    message = FakeMessage(id=9, channel=FakeChannel(id=777, parent_id=111))
    await handle_message(message, MAPPING, forward)
    assert sent[0]["origin"]["channel_id"] == "111"


async def test_make_channels_caches_and_falls_back_to_the_stale_list():
    calls = []
    state = {"down": False}

    def handler(request):
        calls.append(request)
        if state["down"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"channels": [{"name": "fleet"}, {"name": "ops"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        channels = make_channels(client, HUB, "t", ttl=0.05)
        assert await channels() == {"fleet", "ops"}
        assert await channels() == {"fleet", "ops"} and len(calls) == 1  # cached
        await asyncio.sleep(0.07)
        state["down"] = True
        assert await channels() == {"fleet", "ops"}  # a stale list beats none
        assert len(calls) == 2


async def test_make_channels_is_none_when_the_hub_was_never_reachable():
    def down(request):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        assert await make_channels(client, HUB, "t")() is None


async def test_targeting_a_hub_channel_end_to_end(authed):
    client, _app = authed
    auth = {"Authorization": "Bearer test-token"}
    await client.post("/api/v1/channels", json={"name": "ops"}, headers=auth)
    message = FakeMessage(id=55, content="@builder #ops deploy please")
    result = await handle_message(
        message,
        {111: entry("fleet", "ops")},
        make_forward(client, HUB, "test-token"),
        channels=make_channels(client, HUB, "test-token"),
    )
    assert result == "sent"
    ops = (await client.get("/api/v1/messages", params={"channel": "ops"}, headers=auth)).json()["messages"]
    assert [m["text"] for m in ops] == ["@builder deploy please"]
    assert ops[0]["origin"] == {
        "source": "discord",
        "channel_id": "111",
        "message_id": "55",
        "author": "Andrew",
    }
    assert ops[0]["mentions"] == ["builder"]
