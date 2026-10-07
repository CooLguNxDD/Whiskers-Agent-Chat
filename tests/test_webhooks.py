"""Webhooks: filters, formatting, outbound delivery, the inbound receiver, management."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx

from cat_fleet_chat import mcp_app, webhooks
from cat_fleet_chat.db import Database
from cat_fleet_chat.store import redact_url
from cat_fleet_chat.webhooks import (
    CHAT_LIMIT,
    WebhookDispatcher,
    chat_payloads,
    classify,
    event_line,
    generic_request,
    matches,
    sign,
)

DISCORD_URL = "https://discord.test/api/webhooks/1/TOKEN123"


# ---- helpers -----------------------------------------------------------


async def until(predicate, timeout: float = 3.0) -> bool:
    """Poll ``predicate`` (sync or async) until it is truthy or the timeout passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.02)
    return False


async def settle() -> None:
    """Give the dispatcher time to do anything it is going to do."""
    await asyncio.sleep(0.25)


def msg_event(id=1, channel="fleet", author="codex", text="hi", mentions=()):
    return {
        "id": id,
        "kind": "message.created",
        "channel_id": 1,
        "channel": channel,
        "entity_id": id,
        "payload": {
            "id": id,
            "channel": channel,
            "author": author,
            "text": text,
            "mentions": list(mentions),
            "reply_to": None,
            "attachments": [],
        },
        "created_at": "t",
    }


def task_event(id=2, channel="fleet", status="claimed", prev="open", actor="codex", assignee="codex"):
    return {
        "id": id,
        "kind": "task.updated",
        "channel_id": 1,
        "channel": channel,
        "entity_id": 7,
        "payload": {
            "id": 7,
            "title": "Ship it",
            "status": status,
            "assignee": assignee,
            "channel": channel,
            "events": [{"actor": actor, "from_status": prev, "to_status": status, "note": ""}],
        },
        "created_at": "t",
    }


def channel_event(id=3, kind="channel.state_changed"):
    return {
        "id": id,
        "kind": kind,
        "channel_id": 2,
        "channel": "ops",
        "entity_id": 2,
        "payload": {
            "name": "ops",
            "state": "blocked",
            "previous_state": "active",
            "actor": "claude",
            "note": "waiting on infra",
        },
        "created_at": "t",
    }


def hook(**over):
    base = {
        "id": 1,
        "name": "h",
        "direction": "out",
        "url": DISCORD_URL,
        "format": "discord",
        "secret": "s3cret",
        "kinds": [],
        "channels": [],
        "mentions": [],
        "exclude_authors": [],
    }
    return {**base, **over}


async def make_out(client, name="discord", **extra):
    body = {"name": name, "direction": "out", "url": DISCORD_URL, "format": "discord", **extra}
    response = await client.post("/api/v1/webhooks", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def make_in(client, name="inbox", **extra):
    body = {"name": name, "direction": "in", "channel": "fleet", **extra}
    response = await client.post("/api/v1/webhooks", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def post_message(client, text="hi", author="codex", channel="fleet"):
    response = await client.post(
        "/api/v1/messages", json={"channel": channel, "author": author, "text": text}
    )
    assert response.status_code == 201, response.text
    return response.json()["message"]


async def state(client, hook_id):
    response = await client.get(f"/api/v1/webhooks/{hook_id}")
    assert response.status_code == 200, response.text
    return response.json()["webhook"]


def bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


# ---- pure: matching ----------------------------------------------------


def test_empty_filters_match_everything():
    for event in (msg_event(), task_event(), channel_event()):
        assert matches(hook(), event)


def test_kinds_and_channels_filters():
    assert matches(hook(kinds=["message.created"]), msg_event())
    assert not matches(hook(kinds=["message.created"]), task_event())
    assert matches(hook(channels=["fleet"]), msg_event())
    assert not matches(hook(channels=["ops"]), msg_event())


def test_mentions_filter_covers_messages_and_assignments_but_not_channel_broadcasts():
    watching = hook(mentions=["builder"])
    assert matches(watching, msg_event(mentions=["builder"]))
    assert not matches(watching, msg_event(mentions=["someone"]))
    # Assigned to builder and changed by somebody else.
    assert matches(watching, task_event(assignee="builder", actor="claude"))
    assert not matches(watching, task_event(assignee="builder", actor="builder"))
    # agent_relevant() broadcasts channel events; a mentions filter must not.
    assert not matches(watching, channel_event())


def test_exclude_authors_uses_patterns_on_the_actor():
    quiet = hook(exclude_authors=["dc-*"])
    assert not matches(quiet, msg_event(author="dc-andrew"))
    assert matches(quiet, msg_event(author="codex"))
    assert not matches(quiet, task_event(actor="dc-andrew"))
    assert matches(quiet, channel_event())


# ---- pure: formatting --------------------------------------------------


def test_event_lines():
    assert event_line(msg_event(text="ship it")) == "**#fleet** `codex`: ship it"
    line = event_line(task_event())
    assert "task #7" in line and "open → claimed" in line and "by `codex`" in line
    assert "created as open" in event_line(task_event(status="open", prev=None, assignee=None))
    state_line = event_line(channel_event())
    assert "active → blocked" in state_line and "waiting on infra" in state_line


def test_discord_payload_blocks_mass_mentions():
    [payload] = chat_payloads([msg_event(text="@everyone wake up")], "discord")
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["username"] == "Cat Fleet"
    assert "wake up" in payload["content"]


def test_chat_payloads_coalesce_and_respect_the_length_limit():
    events = [msg_event(id=i, text="x" * 300) for i in range(1, 31)]
    payloads = chat_payloads(events, "discord")
    assert len(payloads) > 1
    assert all(len(p["content"]) <= CHAT_LIMIT for p in payloads)
    assert sum(p["content"].count("`codex`:") for p in payloads) == 30
    [single] = chat_payloads([msg_event(), msg_event(id=2)], "discord")
    assert single["content"].count("\n") == 1


def test_slack_payload_uses_slack_bold_and_defangs_pings():
    [payload] = chat_payloads([msg_event(text="<!channel> and <@U123>")], "slack")
    assert set(payload) == {"text"}
    assert "**" not in payload["text"] and "*#fleet*" in payload["text"]
    assert "<!channel>" not in payload["text"] and "<@U123>" not in payload["text"]


def test_generic_request_is_signed_over_the_body():
    request = generic_request(hook(format="generic"), msg_event(id=9))
    headers = request["headers"]
    assert headers["X-CatFleet-Event"] == "message.created"
    assert headers["X-CatFleet-Delivery"] == "9"
    assert headers["X-CatFleet-Signature"] == sign("s3cret", request["content"])


def test_classify_outcomes():
    def outcome(status, **kw):
        return classify(httpx.Response(status, **kw), hook())

    assert outcome(204).kind == "ok"
    assert outcome(500).kind == "retry"
    assert outcome(400).kind == "skip"
    assert outcome(422).kind == "skip"
    for status in (401, 403, 404, 410):
        assert outcome(status).kind == "disable"
    limited = outcome(429, headers={"retry-after": "7"})
    assert (limited.kind, limited.delay) == ("retry", 7.0)
    assert outcome(429).delay == webhooks.BACKOFF[0]
    assert outcome(429, headers={"retry-after": "99999"}).delay == webhooks.RETRY_AFTER_MAX
    assert "<url>" in outcome(400, text=f"bad {DISCORD_URL}").error
    assert "TOKEN123" not in outcome(400, text=f"bad {DISCORD_URL}").error


# ---- management --------------------------------------------------------


async def test_create_returns_the_secret_once(hooked):
    client, _app, _sink = hooked
    created = await make_out(client)
    secret = created["secret"]
    assert len(secret) >= 32
    assert "secret" not in created["webhook"]
    listed = (await client.get("/api/v1/webhooks")).json()["webhooks"]
    assert [h["name"] for h in listed] == ["discord"]
    assert secret not in (await client.get("/api/v1/webhooks")).text
    assert secret not in (await client.get(f"/api/v1/webhooks/{created['webhook']['id']}")).text
    # Direction-specific fields only.
    assert "channel" not in listed[0] and listed[0]["format"] == "discord"


def test_redact_url_keeps_only_scheme_host_and_port():
    assert redact_url(DISCORD_URL) == "https://discord.test/…"
    assert redact_url("http://127.0.0.1:8798/generic?k=v") == "http://127.0.0.1:8798/…"
    assert redact_url("https://user:pw@hooks.test:8443/a/b") == "https://hooks.test:8443/…"
    assert redact_url("http://[::1]:9/x") == "http://[::1]:9/…"


async def test_api_output_never_includes_the_receiver_url_token(hooked):
    """A Discord or Slack URL carries its token, so every response shows it redacted."""
    client, _app, sink = hooked
    created = await make_out(client)
    hook_id = created["webhook"]["id"]
    assert created["webhook"]["url"] == "https://discord.test/…"
    new_url = "https://discord.test/api/webhooks/2/ROTATED456"
    patched = await client.patch(f"/api/v1/webhooks/{hook_id}", json={"url": new_url})
    responses = [
        created,
        patched.json(),
        (await client.get("/api/v1/webhooks")).json(),
        (await client.get(f"/api/v1/webhooks/{hook_id}")).json(),
        (await client.post(f"/api/v1/webhooks/{hook_id}/rotate-secret")).json(),
        await mcp_app.list_webhooks(),
    ]
    dumped = json.dumps(responses)
    assert "TOKEN123" not in dumped and "ROTATED456" not in dumped
    # Redaction is display only: the real URL is still what gets called.
    await post_message(client, "to the new url")
    assert await until(lambda: sink.requests)
    assert str(sink.requests[0].url) == new_url


async def test_create_validation(hooked):
    client, _app, _sink = hooked

    async def create(**body):
        return await client.post("/api/v1/webhooks", json=body)

    assert (await create(name="Bad Name", direction="out", url=DISCORD_URL)).status_code == 400
    assert (await create(name="a", direction="sideways")).status_code == 400
    assert (await create(name="a", direction="out")).status_code == 400  # url required
    assert (await create(name="a", direction="out", url="http://example.com/x")).status_code == 400
    assert (await create(name="a", direction="out", url="ftp://example.com/x")).status_code == 400
    assert (await create(name="a", direction="out", url=DISCORD_URL, format="irc")).status_code == 400
    assert (await create(name="a", direction="out", url=DISCORD_URL, kinds=["nope"])).status_code == 400
    assert (await create(name="a", direction="in")).status_code == 400  # channel required
    assert (await create(name="a", direction="in", channel="ghost")).status_code == 404
    # Plain http is fine for a loopback receiver.
    assert (await create(name="local", direction="out", url="http://127.0.0.1:9/x")).status_code == 201
    assert (await create(name="local", direction="out", url="http://127.0.0.1:9/y")).status_code == 409


async def test_management_requires_the_fleet_token(hooked):
    client, _app, _sink = hooked
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app), base_url="http://testserver"
    ) as anonymous:
        assert (await anonymous.get("/api/v1/webhooks")).status_code == 401
        assert (await anonymous.post("/api/v1/webhooks", json={})).status_code == 401


async def test_update_toggle_and_reject_unknown_fields(hooked):
    client, _app, _sink = hooked
    hook_id = (await make_out(client))["webhook"]["id"]
    patched = await client.patch(
        f"/api/v1/webhooks/{hook_id}",
        json={"kinds": ["task.updated"], "exclude_authors": "dc-*", "enabled": False},
    )
    assert patched.status_code == 200
    body = patched.json()["webhook"]
    assert body["kinds"] == ["task.updated"]
    assert body["exclude_authors"] == ["dc-*"]
    assert body["enabled"] is False
    bad = await client.patch(f"/api/v1/webhooks/{hook_id}", json={"channel": "fleet"})
    assert bad.status_code == 400  # an inbound-only field
    assert (await client.patch(f"/api/v1/webhooks/{hook_id}", json={"name": "x"})).status_code == 400
    assert (await client.patch("/api/v1/webhooks/999", json={})).status_code == 404


async def test_delete_and_rotate(hooked):
    client, _app, _sink = hooked
    created = await make_in(client)
    hook_id, old = created["webhook"]["id"], created["secret"]
    rotated = (await client.post(f"/api/v1/webhooks/{hook_id}/rotate-secret")).json()
    assert rotated["secret"] != old
    url = "/hooks/in/inbox"
    assert (await client.post(url, json={"text": "x"}, headers=bearer(old))).status_code == 401
    assert (
        await client.post(url, json={"text": "x"}, headers=bearer(rotated["secret"]))
    ).status_code == 201
    assert (await client.delete(f"/api/v1/webhooks/{hook_id}")).json()["deleted"] is True
    assert (await client.post(url, json={"text": "x"}, headers=bearer(rotated["secret"]))).status_code == 404
    assert (await client.get(f"/api/v1/webhooks/{hook_id}")).status_code == 404


# ---- inbound -----------------------------------------------------------


async def test_inbound_posts_and_wakes_the_mentioned_agent(hooked):
    client, app, _sink = hooked
    secret = (await make_in(client))["secret"]
    waiting = asyncio.create_task(app.state.hub.wait_for_mentions("builder", 0, timeout=5))
    await asyncio.sleep(0.05)
    # The hook secret is not the fleet token: proves the route sits outside Guard.
    response = await client.post(
        "/hooks/in/inbox", json={"text": "@builder please look"}, headers=bearer(secret)
    )
    assert response.status_code == 201
    message = response.json()["message"]
    assert (message["channel"], message["author"]) == ("fleet", "hook-inbox")
    woken = await asyncio.wait_for(waiting, 3)
    assert [m["text"] for m in woken["messages"]] == ["@builder please look"]


async def test_inbound_accepts_hmac_signature_and_content_alias(hooked):
    client, _app, _sink = hooked
    secret = (await make_in(client))["secret"]
    raw = b'{"content": "from a discord-shaped sender"}'
    response = await client.post(
        "/hooks/in/inbox",
        content=raw,
        headers={"X-CatFleet-Signature": sign(secret, raw), "Content-Type": "application/json"},
    )
    assert response.status_code == 201
    assert response.json()["message"]["text"] == "from a discord-shaped sender"
    forged = await client.post(
        "/hooks/in/inbox",
        content=raw,
        headers={"X-CatFleet-Signature": sign("wrong", raw), "Content-Type": "application/json"},
    )
    assert forged.status_code == 401


async def test_inbound_rejects_bad_callers_without_leaking_which_hooks_exist(hooked):
    client, _app, _sink = hooked
    secret = (await make_in(client))["secret"]
    out = await make_out(client)
    url = "/hooks/in/inbox"
    assert (await client.post(url, json={"text": "x"}, headers=bearer("nope"))).status_code == 401
    assert (await client.post(url, json={"text": "x"}, headers={"Authorization": ""})).status_code == 401
    assert (await client.post("/hooks/in/missing", json={"text": "x"}, headers=bearer(secret))).status_code == 404
    # An outbound hook is not an inbound endpoint, even with its own secret.
    assert (
        await client.post("/hooks/in/discord", json={"text": "x"}, headers=bearer(out["secret"]))
    ).status_code == 404
    await client.patch(f"/api/v1/webhooks/{(await state(client, 1))['id']}", json={"enabled": False})
    assert (await client.post(url, json={"text": "x"}, headers=bearer(secret))).status_code == 404


async def test_inbound_body_rules(hooked):
    client, _app, _sink = hooked
    secret = (await make_in(client))["secret"]
    url = "/hooks/in/inbox"
    headers = {**bearer(secret), "Content-Type": "application/json"}
    assert (await client.post(url, content=b"not json", headers=headers)).status_code == 400
    assert (await client.post(url, content=b"[1]", headers=headers)).status_code == 400
    assert (await client.post(url, json={}, headers=bearer(secret))).status_code == 400  # no text
    huge = b'{"text": "' + b"x" * (70 * 1024) + b'"}'
    assert (await client.post(url, content=huge, headers=headers)).status_code == 413


async def test_inbound_ignores_channel_and_author_unless_override_is_allowed(hooked):
    client, _app, _sink = hooked
    await client.post("/api/v1/channels", json={"name": "ops"})
    fixed = (await make_in(client, "fixed"))["secret"]
    free = (await make_in(client, "free", allow_override=True))["secret"]
    body = {"text": "hello", "channel": "ops", "author": "someone"}
    locked = (await client.post("/hooks/in/fixed", json=body, headers=bearer(fixed))).json()["message"]
    assert (locked["channel"], locked["author"]) == ("fleet", "hook-fixed")
    opened = (await client.post("/hooks/in/free", json=body, headers=bearer(free))).json()["message"]
    assert (opened["channel"], opened["author"]) == ("ops", "someone")


async def test_inbound_client_request_id_is_idempotent_per_hook(hooked):
    client, _app, _sink = hooked
    first = (await make_in(client, "first"))["secret"]
    second = (await make_in(client, "second"))["secret"]
    body = {"text": "once", "client_request_id": "evt-1"}
    a = await client.post("/hooks/in/first", json=body, headers=bearer(first))
    b = await client.post("/hooks/in/first", json=body, headers=bearer(first))
    assert a.status_code == b.status_code == 201
    assert a.json()["message"]["id"] == b.json()["message"]["id"]
    # The same key on another hook is a different request, not a conflict.
    c = await client.post("/hooks/in/second", json=body, headers=bearer(second))
    assert c.status_code == 201 and c.json()["message"]["id"] != a.json()["message"]["id"]
    count = (await client.get("/api/v1/messages", params={"channel": "fleet"})).json()["messages"]
    assert len(count) == 2


async def test_inbound_to_an_archived_channel_is_refused(hooked):
    client, _app, _sink = hooked
    await client.post("/api/v1/channels", json={"name": "old"})
    secret = (await make_in(client, "legacy", channel="old"))["secret"]
    await client.post("/api/v1/channels/old/archive", json={"actor": "claude"})
    response = await client.post("/hooks/in/legacy", json={"text": "x"}, headers=bearer(secret))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "channel_archived"


# ---- outbound delivery -------------------------------------------------


async def test_new_hook_does_not_replay_history(hooked):
    client, _app, sink = hooked
    await post_message(client, "before the hook existed")
    await make_out(client)
    await post_message(client, "after")
    assert await until(lambda: sink.requests)
    await settle()
    [body] = sink.bodies()
    assert "after" in body["content"] and "before" not in body["content"]
    assert sink.requests[0].url == DISCORD_URL


async def test_discord_delivery_covers_messages_tasks_and_channel_state(hooked):
    client, _app, sink = hooked
    await make_out(client)
    await post_message(client, "hello fleet")
    task = (
        await client.post("/api/v1/tasks", json={"channel": "fleet", "title": "Ship it", "actor": "codex"})
    ).json()["task"]
    await client.post(
        f"/api/v1/tasks/{task['id']}/claim", json={"actor": "claude", "expected_version": task["version"]}
    )
    await client.post("/api/v1/channels/fleet/state", json={"state": "review", "actor": "claude"})
    seen = lambda: "review" in "".join(b["content"] for b in sink.bodies())  # noqa: E731
    assert await until(seen)
    text = "\n".join(b["content"] for b in sink.bodies())
    assert "`codex`: hello fleet" in text
    assert 'task #' in text and "Ship it" in text and "open → claimed" in text
    assert "active → review" in text
    assert all(b["allowed_mentions"] == {"parse": []} for b in sink.bodies())


async def test_filters_apply_to_live_delivery(hooked):
    client, _app, sink = hooked
    await client.post("/api/v1/channels", json={"name": "ops"})
    await make_out(client, kinds=["message.created"], channels=["ops"], exclude_authors=["dc-*"])
    await post_message(client, "wrong channel", channel="fleet")
    await post_message(client, "relayed from discord", author="dc-andrew", channel="ops")
    await post_message(client, "this one", channel="ops")
    assert await until(lambda: sink.requests)
    await settle()
    text = "\n".join(b["content"] for b in sink.bodies())
    assert "this one" in text
    assert "wrong channel" not in text and "relayed from discord" not in text


async def test_generic_format_sends_one_signed_request_per_event(hooked):
    client, _app, sink = hooked
    created = await make_out(client, "plain", format="generic", url="https://hooks.test/in")
    await post_message(client, "one")
    await post_message(client, "two")
    assert await until(lambda: len(sink.requests) == 2)
    for request in sink.requests:
        assert request.headers["X-CatFleet-Event"] == "message.created"
        assert request.headers["X-CatFleet-Signature"] == sign(created["secret"], request.content)
    assert [b["payload"]["text"] for b in sink.bodies()] == ["one", "two"]
    assert [r.headers["X-CatFleet-Delivery"] for r in sink.requests] == [
        str(b["id"]) for b in sink.bodies()
    ]


async def test_cursor_advances_only_after_a_2xx(hooked, monkeypatch):
    client, _app, sink = hooked
    monkeypatch.setattr(webhooks, "BACKOFF", (0.05,))
    sink.replies = [500]
    hook_id = (await make_out(client))["webhook"]["id"]
    message = await post_message(client, "retry me")
    assert await until(lambda: len(sink.requests) >= 2)
    first, second = sink.bodies()[:2]
    assert first == second  # the failed batch is resent unchanged
    ok = lambda: state(client, hook_id)  # noqa: E731

    async def delivered():
        current = await ok()
        return current["last_status"] == "ok" and current["failure_count"] == 0

    assert await until(delivered)
    current = await state(client, hook_id)
    assert current["cursor"] >= message["id"]
    assert current["last_error"] is None
    await settle()
    assert len(sink.requests) == 2  # and nothing is resent after success


async def test_5xx_is_recorded_while_retrying(hooked, monkeypatch):
    client, _app, sink = hooked
    monkeypatch.setattr(webhooks, "BACKOFF", (30.0,))  # long enough to observe the failed state
    sink.default = 503
    hook_id = (await make_out(client))["webhook"]["id"]
    await post_message(client, "x")

    async def failing():
        return (await state(client, hook_id))["failure_count"] == 1

    assert await until(failing)
    current = await state(client, hook_id)
    assert current["enabled"] is True
    assert current["last_status"] == "retry:503"
    assert current["last_error"].startswith("HTTP 503")


async def test_network_errors_retry_and_never_store_the_url(hooked, monkeypatch):
    client, _app, sink = hooked
    monkeypatch.setattr(webhooks, "BACKOFF", (30.0,))
    sink.fail_with = httpx.ConnectError(f"cannot reach {DISCORD_URL}")
    hook_id = (await make_out(client))["webhook"]["id"]
    await post_message(client, "x")

    async def failing():
        return (await state(client, hook_id))["failure_count"] == 1

    assert await until(failing)
    current = await state(client, hook_id)
    assert current["enabled"] is True and current["last_status"] == "retry:error"
    assert "TOKEN123" not in current["last_error"] and "<url>" in current["last_error"]


async def test_a_gone_receiver_disables_the_hook(hooked):
    client, _app, sink = hooked
    sink.replies = [404]
    sink.error_text = f"unknown webhook {DISCORD_URL}"
    hook_id = (await make_out(client))["webhook"]["id"]
    await post_message(client, "x")

    async def disabled():
        return (await state(client, hook_id))["enabled"] is False

    assert await until(disabled)
    current = await state(client, hook_id)
    assert current["last_status"] == "disabled:404"
    assert "TOKEN123" not in current["last_error"]
    await post_message(client, "y")
    await settle()
    assert len(sink.requests) == 1  # a disabled hook is left alone
    # Re-enabling clears the error and resumes from the saved cursor.
    sink.replies = []
    await client.patch(f"/api/v1/webhooks/{hook_id}", json={"enabled": True})
    assert await until(lambda: len(sink.requests) >= 2)
    resumed = await state(client, hook_id)
    assert resumed["enabled"] is True and resumed["last_error"] is None


async def test_a_rejected_payload_is_skipped_not_retried(hooked):
    client, _app, sink = hooked
    sink.replies = [400]
    hook_id = (await make_out(client))["webhook"]["id"]
    await post_message(client, "bad one")

    async def skipped():
        current = await state(client, hook_id)
        return current["last_status"] == "400"

    assert await until(skipped)
    assert (await state(client, hook_id))["enabled"] is True
    await settle()
    assert len(sink.requests) == 1  # not resent
    await post_message(client, "next one")
    assert await until(lambda: len(sink.requests) == 2)
    assert "next one" in sink.bodies()[1]["content"]


async def test_disabled_and_deleted_hooks_stop_receiving(hooked):
    client, _app, sink = hooked
    hook_id = (await make_out(client))["webhook"]["id"]
    await client.patch(f"/api/v1/webhooks/{hook_id}", json={"enabled": False})
    await post_message(client, "while disabled")
    await settle()
    assert sink.requests == []
    await client.delete(f"/api/v1/webhooks/{hook_id}")
    await post_message(client, "after delete")
    await settle()
    assert sink.requests == []


async def test_events_in_a_burst_are_coalesced(hooked):
    client, _app, sink = hooked
    await make_out(client)
    for index in range(12):
        await post_message(client, f"burst {index}")
    assert await until(lambda: "burst 11" in "".join(b["content"] for b in sink.bodies()))
    await settle()
    text = "\n".join(b["content"] for b in sink.bodies())
    assert all(f"burst {i}" in text for i in range(12))
    assert len(sink.requests) < 12


# ---- test ping ---------------------------------------------------------


async def test_test_endpoint_sends_a_ping_without_touching_the_cursor(hooked):
    client, _app, sink = hooked
    hook_id = (await make_out(client))["webhook"]["id"]
    before = (await state(client, hook_id))["cursor"]
    result = await client.post(f"/api/v1/webhooks/{hook_id}/test")
    assert result.status_code == 200
    assert result.json() == {"ok": True, "status": 204, "error": None}
    [body] = sink.bodies()
    assert "is connected" in body["content"] and "discord" in body["content"]
    assert (await state(client, hook_id))["cursor"] == before


async def test_test_endpoint_reports_receiver_failure(hooked):
    client, _app, sink = hooked
    hook_id = (await make_out(client))["webhook"]["id"]
    sink.replies = [500]
    result = (await client.post(f"/api/v1/webhooks/{hook_id}/test")).json()
    assert result["ok"] is False and result["status"] == 500


async def test_test_endpoint_rejects_inbound_hooks(hooked):
    client, _app, _sink = hooked
    hook_id = (await make_in(client))["webhook"]["id"]
    assert (await client.post(f"/api/v1/webhooks/{hook_id}/test")).status_code == 400
    assert (await client.post("/api/v1/webhooks/999/test")).status_code == 404


async def test_kill_switch_disables_outbound_but_not_inbound(running):
    client, app = running
    assert app.state.webhooks is None
    out = await client.post(
        "/api/v1/webhooks", json={"name": "d", "direction": "out", "url": DISCORD_URL}
    )
    assert out.status_code == 201
    assert (await client.post(f"/api/v1/webhooks/{out.json()['webhook']['id']}/test")).status_code == 409
    secret = (await make_in(client))["secret"]
    assert (await client.post("/hooks/in/inbox", json={"text": "x"}, headers=bearer(secret))).status_code == 201


async def test_dispatcher_stop_leaves_no_waiter(running):
    _client, app = running
    hub = app.state.hub
    dispatcher = WebhookDispatcher(hub, transport=httpx.MockTransport(lambda r: httpx.Response(204)))
    dispatcher.start()
    assert await until(lambda: hub.waiters.waiter_count == 1)
    await dispatcher.stop()
    assert hub.waiters.waiter_count == 0


# ---- MCP ---------------------------------------------------------------


async def test_mcp_tools_manage_webhooks(hooked):
    _client, _app, sink = hooked
    created = await mcp_app.create_webhook(
        "ops-feed", "out", url=DISCORD_URL, format="discord", kinds=["task.updated"]
    )
    assert created["secret"] and created["webhook"]["kinds"] == ["task.updated"]
    listed = await mcp_app.list_webhooks()
    assert [h["name"] for h in listed["webhooks"]] == ["ops-feed"]
    assert created["secret"] not in str(listed)
    assert (await mcp_app.list_webhooks("in"))["webhooks"] == []
    updated = await mcp_app.update_webhook("ops-feed", {"channels": ["fleet"]})
    assert updated["webhook"]["channels"] == ["fleet"]
    assert (await mcp_app.test_webhook("ops-feed"))["ok"] is True
    assert len(sink.requests) == 1
    assert (await mcp_app.delete_webhook("ops-feed"))["deleted"] is True
    assert "error" in await mcp_app.delete_webhook("ops-feed")
    assert "error" in await mcp_app.create_webhook("bad name", "out", url=DISCORD_URL)


# ---- schema upgrade ----------------------------------------------------


async def test_database_without_the_webhooks_table_upgrades_in_place(tmp_path):
    path = tmp_path / "v3.sqlite"
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE webhooks")
        conn.execute("DELETE FROM schema_migrations WHERE version = 4")
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert "webhooks" in tables
    assert versions == [1, 2, 3, 4]
