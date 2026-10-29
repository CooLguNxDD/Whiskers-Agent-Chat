"""Slack and Telegram adapters against a real hub, with a fake Slack web client and a mocked Bot API."""

from __future__ import annotations

import json

import httpx
import pytest

from cat_fleet_chat.relay.core import RelayCore
from cat_fleet_chat.relay.hub_client import HubClient
from cat_fleet_chat.relay.render import Button, Rendered, render
from cat_fleet_chat.relay.slack_adapter import SlackAdapter, blocks, slack_text
from cat_fleet_chat.relay.telegram_adapter import TelegramAdapter, message_html, telegram_html

ADMIN = {"Authorization": "Bearer test-token"}


async def make_core(client, adapter, name, platform, external_id):
    created = await client.post(
        "/api/v1/relays",
        json={
            "actor": "ann",
            "name": name,
            "platform": platform,
            "config": {"operators": ["U1", "11"], "action_users": ["U1", "11"]},
        },
        headers=ADMIN,
    )
    assert created.status_code == 201, created.text
    bound = await client.post(
        f"/api/v1/relays/{name}/bindings",
        json={"actor": "ann", "external_id": external_id, "hub_channel": "fleet"},
        headers=ADMIN,
    )
    assert bound.status_code == 201, bound.text
    core = RelayCore(HubClient(client, "http://testserver", created.json()["token"], name), adapter)
    adapter.attach(core)
    assert await core.load()
    return core


# --- Slack -----------------------------------------------------------------


class FakeWeb:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, method):
        async def call(**kwargs):
            self.calls.append((method, kwargs))
            if method == "users_info":
                return {"user": {"name": "ann", "profile": {"display_name": "Ann"}}}
            return {"ts": f"1700.{len(self.calls):04d}"}

        return call

    def named(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]


@pytest.fixture
async def slack(authed):
    client, app = authed
    web = FakeWeb()
    adapter = SlackAdapter(web)
    adapter.bot_user = "UBOT"
    core = await make_core(client, adapter, "team", "slack", "C1")
    yield adapter, core, app.state.hub, web


def test_slack_text_defangs_and_converts_bold():
    assert slack_text("**hi** <!channel> <@U2>") == "*hi* <​!channel> <​@U2>"


def test_slack_blocks_carry_buttons():
    message = Rendered(text="x", title="T", body="b", fields=(("k", "v"),), footer="f",
                       buttons=(Button("Approve", "cf1:approve:proposal:P1", "success"),))
    built = blocks(message)
    assert [block["type"] for block in built] == ["section", "section", "section", "actions", "context"]
    assert built[3]["elements"][0]["action_id"] == "cf1:approve:proposal:P1"
    assert built[3]["elements"][0]["style"] == "primary"


async def test_slack_message_relays_threads_and_marks(slack):
    adapter, _core, hub, web = slack
    assert await adapter.on_event({"type": "message", "channel": "C1", "user": "U2", "ts": "1.1", "text": "hi"}) == "sent"
    posted = (await hub.get_messages("fleet"))["messages"][-1]
    assert posted["author"] == "sl-Ann" and posted["origin"]["source"] == "slack"
    assert web.named("reactions_add")[-1]["name"] == "white_check_mark"
    threaded = {"type": "message", "channel": "C1", "user": "U2", "ts": "1.3", "thread_ts": "1.1", "text": "more"}
    assert await adapter.on_event(threaded) == "sent"
    assert (await hub.get_messages("fleet"))["messages"][-1]["origin"]["thread_id"] == "C1:1.1"
    for ignored in (
        {"type": "message", "channel": "C1", "bot_id": "B1", "ts": "1.4", "text": "bot"},
        {"type": "message", "channel": "C1", "user": "UBOT", "ts": "1.5", "text": "me"},
        {"type": "message", "subtype": "message_changed", "channel": "C1", "user": "U2", "ts": "1.6"},
        {"type": "message", "channel": "C9", "user": "U2", "ts": "1.7", "text": "unbound"},
    ):
        assert await adapter.on_event(ignored) == "ignored"


async def test_slack_send_into_thread_and_reply_route(slack):
    adapter, core, hub, web = slack
    await adapter.on_event({"type": "message", "channel": "C1", "user": "U2", "ts": "2.1", "thread_ts": "2.0", "text": "q?"})
    asked = (await hub.get_messages("fleet"))["messages"][-1]
    await hub.post_message("fleet", "agent", "answer", reply_to=asked["id"])
    assert await core.deliver_once(core.config.replies_consumer, None) is True
    sent = web.named("chat_postMessage")[-1]
    assert sent["channel"] == "C1" and sent["thread_ts"] == "2.0" and sent["text"] == "*agent*: answer"
    thread = await adapter.create_thread("C1", "Task #1")
    assert thread == f"C1:1700.{len(web.calls):04d}"
    assert web.named("chat_postMessage")[-1]["text"] == "🧵 Task #1"


async def test_slack_slash_command_and_buttons(slack):
    adapter, _core, hub, web = slack
    reply = await adapter.on_command({"text": "status", "user_id": "U2", "channel_id": "C1", "user_name": "sam"})
    assert reply.ok and web.named("chat_postEphemeral")[-1]["user"] == "U2"
    hub.human_handles = frozenset({"sl-ann"})
    await hub.create_proposal("builder", "demo", "Ship it", "Summary", 2)
    reject = {"type": "block_actions", "user": {"id": "U1", "username": "ann"}, "trigger_id": "t",
              "channel": {"id": "C1"}, "message": {}, "actions": [{"action_id": "cf1:reject:proposal:P1"}]}
    assert await adapter.on_interactive(reject) is None
    modal = web.named("views_open")[-1]["view"]
    assert modal["callback_id"] == "cf1:reject:proposal:P1" and modal["private_metadata"] == "C1"
    submit = {"type": "view_submission", "user": {"id": "U1", "username": "ann"},
              "view": {"callback_id": "cf1:reject:proposal:P1", "private_metadata": "C1",
                       "state": {"values": {"reason": {"reason": {"value": "later"}}}}}}
    done = await adapter.on_interactive(submit)
    assert done.ok, done.text
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "rejected"
    stranger = dict(reject, user={"id": "U9", "username": "eve"})
    assert not (await adapter.on_interactive(stranger)).ok


# --- Telegram --------------------------------------------------------------


class BotApi:
    """Records Bot API calls; answers ok with a running message id."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getUpdates":
            result, self.updates = self.updates, []
        elif method == "createForumTopic":
            result = {"message_thread_id": 77}
        elif method == "sendMessage":
            result = {"message_id": 500 + len(self.calls)}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def named(self, method):
        return [body for name, body in self.calls if name == method]


@pytest.fixture
async def telegram(authed):
    client, app = authed
    api = BotApi()
    tg_client = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    adapter = TelegramAdapter(tg_client, "123456:" + "A" * 35)
    adapter.bot_id, adapter.bot_username = 99, "fleetbot"
    core = await make_core(client, adapter, "tg", "telegram", "-100123")
    yield adapter, core, app.state.hub, api
    await tg_client.aclose()


def message(text, *, message_id=1, user_id=11, topic=None, reply_to=None):
    body = {"message_id": message_id, "chat": {"id": -100123}, "from": {"id": user_id, "username": "ann"}, "text": text}
    if topic:
        body.update(message_thread_id=topic, is_topic_message=True)
    if reply_to:
        body["reply_to_message"] = {"message_id": reply_to}
    return body


def test_telegram_html_escapes_and_defangs():
    assert telegram_html("**a** <b>x</b> @all") == "<b>a</b> &lt;b&gt;x&lt;/b&gt; @​all"
    card = message_html(Rendered(text="x", title="T <1>", body="b", fields=(("k", "v"),), footer="f"))
    assert card.startswith("<b>T &lt;1&gt;</b>") and "<i>f</i>" in card


async def test_telegram_polls_relays_and_commands(telegram):
    adapter, _core, hub, api = telegram
    api.updates = [
        {"update_id": 5, "message": message("hello fleet")},
        {"update_id": 6, "message": message("in topic", message_id=2, topic=7)},
        {"update_id": 7, "message": message("/fleet@fleetbot status", message_id=3)},
        {"update_id": 8, "message": message("from bot", message_id=4, user_id=99)},
    ]
    assert await adapter.poll_once(0) == 4
    assert adapter.offset == 9
    texts = [m["text"] for m in (await hub.get_messages("fleet"))["messages"]]
    assert texts[-2:] == ["hello fleet", "in topic"]
    topic_origin = (await hub.get_messages("fleet"))["messages"][-1]["origin"]
    assert topic_origin["thread_id"] == "-100123:7" and topic_origin["channel_id"] == "-100123"
    answers = api.named("sendMessage")
    assert "Relay <b>tg</b>" in answers[-1]["text"] and answers[-1]["reply_parameters"]["message_id"] == 3
    assert api.named("setMessageReaction")


async def test_telegram_buttons_and_forced_reply_reason(telegram):
    adapter, _core, hub, api = telegram
    hub.human_handles = frozenset({"tg-ann"})
    await hub.create_proposal("builder", "demo", "Ship it", "Summary", 2)
    query = {"id": "q1", "data": "cf1:reject:proposal:P1", "from": {"id": 11, "username": "ann"},
             "message": {"message_id": 40, "chat": {"id": -100123}}}
    assert await adapter.on_callback(query) is None
    prompt_id = 500 + len(api.calls) - 1
    assert api.named("sendMessage")[-1]["reply_markup"]["force_reply"] is True
    assert await adapter.on_message(message("too risky", message_id=41, reply_to=prompt_id)) == "button"
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "rejected"
    bad = await adapter.on_callback({"id": "q2", "data": "evil", "from": {"id": 11}, "message": {"message_id": 1, "chat": {"id": -100123}}})
    assert bad is None and api.named("answerCallbackQuery")[-1]["show_alert"] is True


async def test_telegram_send_card_and_topic(telegram):
    adapter, _core, hub, api = telegram
    task = (await hub.create_task("fleet", "Card", actor="ann"))["task"]
    card = render({"id": 1, "kind": "task.updated", "channel": "fleet", "payload": task}, "card")
    await adapter.send("-100123:7", card)
    sent = api.named("sendMessage")[-1]
    assert sent["message_thread_id"] == 7
    assert sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("cf1:claim:task:")
    assert await adapter.create_thread("-100123", "Task") == "-100123:77"
