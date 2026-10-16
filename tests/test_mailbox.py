"""Agent mailbox: hub rules, wait delivery, events, webhooks, REST, MCP, listener, migration."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from cat_fleet_chat import listen, mcp_app
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import agent_relevant
from cat_fleet_chat.webhooks import chat_payloads, event_line, matches


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


# --- hub rules -------------------------------------------------------------


async def test_send_list_and_count(running):
    _client, app = running
    hub = app.state.hub
    sent = await hub.send_mail("alice", "bob", "please review", subject="review")
    mail = sent["mail"]
    assert mail["sender"] == "alice" and mail["recipient"] == "bob"
    assert mail["subject"] == "review" and mail["body"] == "please review"
    assert mail["read_at"] is None and mail["acked_at"] is None and mail["state"] == "unread"

    listed = await hub.list_mail("bob", state="unread")
    assert [m["id"] for m in listed["mail"]] == [mail["id"]]
    assert listed["cursor"] == mail["id"] and listed["has_more"] is False
    assert (await hub.list_mail("alice", state="all"))["mail"] == []
    assert await hub.mail_counts("bob") == {"agent": "bob", "unread": 1, "unacked": 1, "total": 1}

    # A sender may mail themselves; any valid handle may receive mail.
    selfie = await hub.send_mail("alice", "alice", "note to self")
    assert selfie["mail"]["recipient"] == "alice"
    assert (await hub.send_mail("alice", "never-seen-before", "hi"))["mail"]["id"]
    with pytest.raises(HubError) as bad:
        await hub.send_mail("alice", "not a handle", "hi")
    assert bad.value.code == "validation_error"


async def test_subject_optional_and_capped(running):
    _client, app = running
    hub = app.state.hub
    plain = await hub.send_mail("alice", "bob", "no subject")
    assert plain["mail"]["subject"] == ""
    folded = await hub.send_mail("alice", "bob", "x", subject="  two\nlines  ")
    assert folded["mail"]["subject"] == "two lines"
    exact = await hub.send_mail("alice", "bob", "x", subject="s" * 200)
    assert len(exact["mail"]["subject"]) == 200
    with pytest.raises(HubError) as long:
        await hub.send_mail("alice", "bob", "x", subject="s" * 201)
    assert long.value.code == "validation_error"
    assert long.value.details["field"] == "subject"


async def test_body_required_and_capped(running):
    _client, app = running
    hub = app.state.hub
    for body in ("", "   ", None, "b" * 16_001):
        with pytest.raises(HubError) as bad:
            await hub.send_mail("alice", "bob", body)
        assert bad.value.code == "validation_error"
    assert (await hub.send_mail("alice", "bob", "b" * 16_000))["mail"]["id"]


async def test_read_is_idempotent_and_up_to_id(running):
    _client, app = running
    hub = app.state.hub
    ids = [(await hub.send_mail("alice", "bob", f"m{i}"))["mail"]["id"] for i in range(3)]

    first = await hub.mark_mail_read("bob", ids=[ids[0]])
    assert first["updated"] == [ids[0]] and first["unread"] == 2
    again = await hub.mark_mail_read("bob", ids=[ids[0]])
    assert again["updated"] == [] and again["unread"] == 2

    rest = await hub.mark_mail_read("bob", up_to_id=ids[2])
    assert rest["updated"] == ids[1:] and rest["unread"] == 0
    assert [m["id"] for m in (await hub.list_mail("bob", state="read"))["mail"]] == ids
    assert (await hub.list_mail("bob", state="unread"))["mail"] == []

    with pytest.raises(HubError) as neither:
        await hub.mark_mail_read("bob")
    assert neither.value.code == "validation_error"
    with pytest.raises(HubError) as both:
        await hub.mark_mail_read("bob", ids=[ids[0]], up_to_id=ids[0])
    assert both.value.code == "validation_error"


async def test_ack_implies_read(running):
    _client, app = running
    hub = app.state.hub
    mail_id = (await hub.send_mail("alice", "bob", "act on this"))["mail"]["id"]
    acked = await hub.ack_mail("bob", ids=[mail_id])
    assert acked["updated"] == [mail_id]
    assert acked["unread"] == 0 and acked["unacked"] == 0
    [mail] = (await hub.list_mail("bob", state="all"))["mail"]
    assert mail["read_at"] and mail["acked_at"] and mail["state"] == "acked"
    assert (await hub.ack_mail("bob", ids=[mail_id]))["updated"] == []


async def test_only_recipient_may_read_or_ack(running):
    _client, app = running
    hub = app.state.hub
    mail_id = (await hub.send_mail("alice", "bob", "for bob"))["mail"]["id"]
    for call in (
        hub.mark_mail_read("mallory", ids=[mail_id]),
        hub.ack_mail("mallory", ids=[mail_id]),
        # Not even the sender may mark it.
        hub.ack_mail("alice", ids=[mail_id]),
    ):
        with pytest.raises(HubError) as denied:
            await call
        assert denied.value.code == "forbidden" and denied.value.status == 403
    with pytest.raises(HubError) as missing:
        await hub.ack_mail("bob", ids=[mail_id + 99])
    assert missing.value.code == "not_found"
    assert (await hub.mail_counts("bob"))["unread"] == 1
    # up_to_id only ever touches the caller's own mail.
    assert (await hub.mark_mail_read("mallory", up_to_id=mail_id))["updated"] == []
    assert (await hub.mail_counts("bob"))["unread"] == 1


async def test_reply_to_validation(running):
    _client, app = running
    hub = app.state.hub
    first = (await hub.send_mail("alice", "bob", "question"))["mail"]
    reply = await hub.send_mail("bob", "alice", "answer", reply_to=first["id"])
    assert reply["mail"]["reply_to"] == first["id"]
    # The original sender may follow up on their own mail too.
    assert (await hub.send_mail("alice", "bob", "more", reply_to=first["id"]))["mail"]["reply_to"]
    for sender, parent in (("mallory", first["id"]), ("bob", first["id"] + 99), ("bob", "x")):
        with pytest.raises(HubError) as bad:
            await hub.send_mail(sender, "alice", "hijack", reply_to=parent)
        assert bad.value.code == "validation_error"
        assert bad.value.details["field"] == "reply_to"


async def test_idempotent_send_with_client_request_id(running):
    _client, app = running
    hub = app.state.hub
    first = await hub.send_mail("alice", "bob", "once", subject="s", client_request_id="mail-1")
    again = await hub.send_mail("alice", "bob", "once", subject="s", client_request_id="mail-1")
    assert again["mail"]["id"] == first["mail"]["id"]
    assert (await hub.mail_counts("bob"))["total"] == 1
    with pytest.raises(HubError) as conflict:
        await hub.send_mail("alice", "bob", "different", client_request_id="mail-1")
    assert conflict.value.code == "idempotency_conflict"
    # A key already used for a chat message cannot be reused for mail.
    await hub.post_message("fleet", "alice", "hi", client_request_id_value="shared-key")
    with pytest.raises(HubError) as crossed:
        await hub.send_mail("alice", "bob", "hi", client_request_id="shared-key")
    assert crossed.value.code == "idempotency_conflict"


async def test_list_pages_forward_by_cursor(running):
    _client, app = running
    hub = app.state.hub
    ids = [(await hub.send_mail("alice", "bob", f"m{i}"))["mail"]["id"] for i in range(5)]
    page = await hub.list_mail("bob", state="all", limit=2)
    assert [m["id"] for m in page["mail"]] == ids[:2] and page["has_more"] is True
    page = await hub.list_mail("bob", state="all", since_id=page["cursor"], limit=2)
    assert [m["id"] for m in page["mail"]] == ids[2:4] and page["has_more"] is True
    page = await hub.list_mail("bob", state="all", since_id=page["cursor"], limit=2)
    assert [m["id"] for m in page["mail"]] == ids[4:] and page["has_more"] is False
    with pytest.raises(HubError):
        await hub.list_mail("bob", state="spam")


# --- delivery through wait -------------------------------------------------


async def test_waiting_agent_wakes_on_mail(running):
    _client, app = running
    hub = app.state.hub
    pending = asyncio.create_task(hub.wait_for_mentions("bob", 0, timeout=5))
    await _parked(hub)
    started = time.monotonic()
    sent = (await hub.send_mail("alice", "bob", "wake up", subject="ping"))["mail"]
    woke = await asyncio.wait_for(pending, 2)
    assert time.monotonic() - started < 1.5
    assert woke["timed_out"] is False
    assert woke["messages"] == [] and woke["cursor"] == 0
    assert [m["id"] for m in woke["mail"]] == [sent["id"]]
    assert woke["mail"][0]["body"] == "wake up"
    assert woke["mail_cursor"] == sent["id"] and woke["mail_unread"] == 1


async def test_wait_mail_cursor_has_no_lost_wakeup_and_times_out(running):
    _client, app = running
    hub = app.state.hub
    sent = (await hub.send_mail("alice", "bob", "landed before the wait"))["mail"]
    # Mail that landed between two waits is delivered by the mail cursor at once.
    early = await hub.wait_for_mentions("bob", 0, timeout=5, mail_since_id=0)
    assert [m["id"] for m in early["mail"]] == [sent["id"]]

    # Without a mail cursor, old unread mail does not wake again (no busy loop),
    # but the count still says it is there, and the timeout still applies.
    started = time.monotonic()
    quiet = await hub.wait_for_mentions("bob", 0, timeout=1)
    assert 0.8 <= time.monotonic() - started < 2.5
    assert quiet["timed_out"] is True and quiet["mail"] == []
    assert quiet["mail_unread"] == 1 and quiet["mail_cursor"] == sent["id"]

    # The returned cursor sent back waits for new mail only.
    resumed = await hub.wait_for_mentions(
        "bob", 0, timeout=1, mail_since_id=early["mail_cursor"]
    )
    assert resumed["timed_out"] is True and resumed["mail_cursor"] == sent["id"]

    # Mail for someone else never wakes bob.
    other = asyncio.create_task(hub.wait_for_mentions("bob", 0, timeout=1))
    await _parked(hub)
    await hub.send_mail("alice", "carol", "not for bob")
    assert (await asyncio.wait_for(other, 3))["timed_out"] is True


async def test_mentions_still_wake_as_before(running):
    client, app = running
    hub = app.state.hub
    pending = asyncio.create_task(hub.wait_for_mentions("codex", 0, timeout=5))
    await _parked(hub)
    posted = (await hub.post_message("fleet", "ada", "go @codex"))["message"]
    woke = await asyncio.wait_for(pending, 2)
    assert [m["id"] for m in woke["messages"]] == [posted["id"]]
    assert woke["cursor"] == posted["id"] and woke["mail"] == []
    # A channel mention is not mail.
    assert (await hub.mail_counts("codex"))["total"] == 0

    # REST wait keeps its shape and accepts the mail cursor.
    response = await client.get(
        "/api/v1/wait",
        params={"agent": "codex", "since_id": posted["id"], "timeout": 1, "mail_since_id": 0},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["timed_out"] is True and body["cursor"] == posted["id"]
    assert body["messages"] == [] and body["mail"] == [] and body["mail_cursor"] == 0


async def test_mail_event_is_logged_without_body(running):
    _client, app = running
    hub = app.state.hub
    sent = (await hub.send_mail("alice", "bob", "secret body text", subject="hello"))["mail"]
    [event] = await hub.events_after(0, channel=None, kinds=frozenset({"mail"}))
    assert event["kind"] == "mail" and event["entity_id"] == sent["id"]
    assert event["channel_id"] is None
    assert "secret body text" not in json.dumps(event)
    assert event["payload"]["sender"] == "alice" and event["payload"]["subject"] == "hello"
    assert agent_relevant(event, "bob") and not agent_relevant(event, "alice")

    # wait_for_events delivers it to the recipient with the full mail attached.
    for_bob = await hub.wait_for_events("bob", 0, timeout=0)
    assert [e["kind"] for e in for_bob["events"]] == ["mail"]
    assert for_bob["mail"][0]["body"] == "secret body text" and for_bob["mail_unread"] == 1
    for_alice = await hub.wait_for_events("alice", 0, timeout=0)
    assert for_alice["events"] == [] and for_alice["mail"] == []
    # Without an agent no mail body is ever attached.
    everyone = await hub.wait_for_events(None, 0, kinds="mail", timeout=0)
    assert len(everyone["events"]) == 1 and everyone["mail"] == []


def test_webhook_formatting_and_matching_skip_mail_bodies():
    event = {
        "id": 9,
        "kind": "mail",
        "channel_id": None,
        "channel": None,
        "entity_id": 3,
        "payload": {"id": 3, "sender": "alice", "recipient": "bob", "subject": "hi", "reply_to": None},
        "created_at": "t",
    }
    line = event_line(event)
    assert "alice" in line and "bob" in line and "hi" in line
    assert chat_payloads([event], "slack")[0]["text"]
    base = {"name": "d", "kinds": [], "channels": [], "mentions": [], "exclude_authors": []}
    assert not matches(base, event)  # an empty filter does not forward private mail notices
    assert matches({**base, "kinds": ["mail"]}, event)
    assert matches({**base, "kinds": ["mail"], "mentions": ["bob"]}, event)
    assert not matches({**base, "kinds": ["mail"], "exclude_authors": ["ali*"]}, event)


async def test_webhook_dispatch_survives_mail(hooked):
    client, _app, sink = hooked
    for body in (
        {"name": "discord", "direction": "out", "url": "https://discord.test/api/webhooks/1/T", "format": "discord"},
        {"name": "audit", "direction": "out", "url": "https://hooks.test/in", "format": "generic", "kinds": ["mail"]},
    ):
        assert (await client.post("/api/v1/webhooks", json=body)).status_code == 201
    sent = await client.post(
        "/api/v1/mailbox",
        json={"sender": "alice", "recipient": "bob", "subject": "s", "body": "private words"},
    )
    assert sent.status_code == 201
    await client.post("/api/v1/messages", json={"channel": "fleet", "author": "ada", "text": "after mail"})
    assert await _until(lambda: len(sink.requests) >= 2)
    await asyncio.sleep(0.25)
    raw = b"".join(request.content for request in sink.requests)
    assert b"private words" not in raw
    generic = [r for r in sink.requests if r.headers.get("X-CatFleet-Event") == "mail"]
    assert len(generic) == 1
    assert json.loads(generic[0].content)["payload"]["recipient"] == "bob"
    discord_text = "\n".join(
        json.loads(r.content)["content"] for r in sink.requests if "discord" in str(r.url)
    )
    assert "after mail" in discord_text and "mail #" not in discord_text


# --- REST ------------------------------------------------------------------


async def test_rest_mailbox_flow(running):
    client, _app = running
    sent = await client.post(
        "/api/v1/mailbox",
        json={"sender": "alice", "recipient": "bob", "body": "via rest", "subject": "rest"},
    )
    assert sent.status_code == 201, sent.text
    mail_id = sent.json()["mail"]["id"]

    listed = await client.get("/api/v1/mailbox/bob", params={"state": "unread"})
    assert [m["id"] for m in listed.json()["mail"]] == [mail_id]
    counts = await client.get("/api/v1/mailbox/bob/counts")
    assert counts.json()["unread"] == 1

    denied = await client.post("/api/v1/mailbox/mallory/ack", json={"ids": [mail_id]})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"

    read = await client.post("/api/v1/mailbox/bob/read", json={"ids": [mail_id]})
    assert read.status_code == 200 and read.json()["unread"] == 0
    acked = await client.post("/api/v1/mailbox/bob/ack", json={"ids": [mail_id]})
    assert acked.status_code == 200 and acked.json()["unacked"] == 0

    bad = await client.post("/api/v1/mailbox", json={"sender": "alice", "recipient": "bob"})
    assert bad.status_code == 400
    bad_state = await client.get("/api/v1/mailbox/bob", params={"state": "nope"})
    assert bad_state.status_code == 400


async def test_rest_mailbox_requires_auth(authed):
    client, _app = authed
    assert (await client.post("/api/v1/mailbox", json={})).status_code == 401
    for path in ("/api/v1/mailbox/bob", "/api/v1/mailbox/bob/counts"):
        assert (await client.get(path)).status_code == 401
    for path in ("/api/v1/mailbox/bob/read", "/api/v1/mailbox/bob/ack"):
        assert (await client.post(path, json={"ids": [1]})).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    ok = await client.post(
        "/api/v1/mailbox",
        headers=headers,
        json={"sender": "alice", "recipient": "bob", "body": "authorized"},
    )
    assert ok.status_code == 201
    cross = await client.post(
        "/api/v1/mailbox",
        headers={**headers, "Origin": "http://evil.example"},
        json={"sender": "alice", "recipient": "bob", "body": "cross"},
    )
    assert cross.status_code == 403


# --- MCP -------------------------------------------------------------------


async def test_mcp_mail_tools_registered_and_work(running):
    names = {tool.name for tool in await mcp_app.build_mcp().list_tools()}
    assert {"fleet_mail_send", "fleet_mail_list", "fleet_mail_read", "fleet_mail_ack"} <= names

    sent = await mcp_app.fleet_mail_send("alice", "bob", "via mcp", subject="mcp")
    mail_id = sent["mail"]["id"]
    listed = await mcp_app.fleet_mail_list("bob")
    assert [m["id"] for m in listed["mail"]] == [mail_id] and listed["unread"] == 1
    denied = await mcp_app.fleet_mail_ack("mallory", [mail_id])
    assert denied["status"] == "error" and denied["error"] == "forbidden"
    assert (await mcp_app.fleet_mail_read("bob", ids=[mail_id]))["unread"] == 0
    assert (await mcp_app.fleet_mail_ack("bob", [mail_id]))["unacked"] == 0
    waited = await mcp_app.wait_for_mentions("bob", 0, timeout=1, mail_since_id=0)
    assert [m["id"] for m in waited["mail"]] == [mail_id]
    bad = await mcp_app.fleet_mail_send("alice", "bob", "")
    assert bad["status"] == "error" and bad["error"] == "validation_error"


# --- listener --------------------------------------------------------------


async def test_listener_prints_mail_once_across_reconnect(running, tmp_path):
    client, app = running
    hub = app.state.hub
    state = tmp_path / "cursor"
    args = listen.build_parser().parse_args(
        [
            "--url", "http://testserver",
            "--agent", "bob",
            "--mail-only",
            "--once",
            "--poll-timeout", "5",
            "--state-file", str(state),
        ]
    )
    seen: list[dict] = []
    task = asyncio.create_task(listen.listen(args, client, emit=seen.append))
    await _parked(hub)
    await hub.post_message("fleet", "ada", "@bob a mention, filtered out by --mail-only")
    await hub.send_mail("alice", "bob", "first letter", subject="one")
    await asyncio.wait_for(task, 5)
    assert [e["kind"] for e in seen] == ["mail"]
    assert seen[0]["mail"]["body"] == "first letter"

    # Reconnect from the saved cursor: the first mail is not replayed.
    await hub.send_mail("alice", "bob", "second letter", subject="two")
    seen.clear()
    await asyncio.wait_for(listen.listen(args, client, emit=seen.append), 5)
    assert [e["mail"]["body"] for e in seen] == ["second letter"]

    # A failed poll keeps the cursor, so mail sent meanwhile arrives once.
    await hub.send_mail("alice", "bob", "third letter")
    real_get = client.get
    calls = {"n": 0}

    async def flaky_get(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("hub restarting")
        return await real_get(*a, **kw)

    client.get = flaky_get
    try:
        seen.clear()

        async def no_sleep(_delay):
            return None

        await asyncio.wait_for(
            listen.listen(args, client, emit=seen.append, sleep=no_sleep), 5
        )
    finally:
        client.get = real_get
    assert [e["mail"]["body"] for e in seen] == ["third letter"]


# --- migration -------------------------------------------------------------


async def test_v5_database_upgrades_on_open(tmp_path):
    path = tmp_path / "v5.sqlite"
    db = Database(str(path))
    await db.open()
    await db.close()
    # Turn the fresh file back into a v5 database: no mailbox, versions 1..5.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE mailbox")
        conn.execute("DELETE FROM schema_migrations WHERE version > 5")
        conn.execute(
            "INSERT INTO events (kind, channel_id, entity_id, payload_json, created_at) "
            "VALUES ('channel.created', 1, 1, '{}', 't')"
        )

    for _ in range(2):  # the second open must be a no-op
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert SCHEMA_VERSION >= 7
    assert "mailbox" in tables
    assert {"idx_mailbox_recipient", "idx_mailbox_unread"} <= indexes
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert events == 1
