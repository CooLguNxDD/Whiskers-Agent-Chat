"""Routing: agents address Discord channels, and replies find their way back."""

from __future__ import annotations

import json
import sqlite3

import httpx

from cat_fleet_chat import mcp_app
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.hub import REPLY_CHAIN_MAX
from cat_fleet_chat.webhooks import chat_payloads, event_line, matches
from test_webhooks import bearer, hook, make_in, msg_event, post_message, settle, until

DISCORD_CHANNEL = "900"


def url_for(name: str) -> str:
    return f"https://discord.test/api/webhooks/1/{name}"


async def make_dest(client, name, **extra):
    """An outbound Discord hook. Its URL ends in its name, so a request can be traced to it."""
    body = {"name": name, "direction": "out", "format": "discord", "url": url_for(name), **extra}
    response = await client.post("/api/v1/webhooks", json=body)
    assert response.status_code == 201, response.text
    return response.json()["webhook"]


def bodies_for(sink, name):
    return [json.loads(r.content) for r in sink.requests if str(r.url).endswith(f"/{name}")]


def text_for(sink, name):
    return "\n".join(body["content"] for body in bodies_for(sink, name))


async def send(client, text, author="builder", **extra):
    response = await client.post(
        "/api/v1/messages", json={"channel": "fleet", "author": author, "text": text, **extra}
    )
    return response


async def discord_request(client, text, channel_id=DISCORD_CHANNEL, message_id="1", author="dc-andrew"):
    """A message as the relay posts it: with an origin."""
    response = await send(
        client,
        text,
        author=author,
        origin={"source": "discord", "channel_id": channel_id, "message_id": message_id, "author": "andrew"},
    )
    assert response.status_code == 201, response.text
    return response.json()["message"]


# ---- pure: matching and formatting ------------------------------------


def addressed(destination, **over):
    event = msg_event(**over)
    event["payload"]["destination"] = destination
    return event


def test_an_addressed_message_matches_its_hook_whatever_the_filters_say():
    narrow = hook(name="alerts", kinds=["task.updated"], channels=["ops"], exclude_authors=["builder"])
    assert matches(narrow, addressed("alerts"))
    assert not matches(narrow, addressed("someone-else"))
    assert not matches(narrow, msg_event())  # not addressed, so the filters apply


def test_a_directed_only_hook_matches_nothing_but_addressed_messages():
    directed = hook(name="help", directed_only=True)
    assert matches(directed, addressed("help"))
    assert not matches(directed, msg_event())
    assert not matches(directed, addressed("other"))


def reply_event():
    event = addressed("help", id=9, text="done, deployed")
    event["payload"]["reply_to"] = 4
    return event


def test_an_addressed_reply_is_quoted_above_the_answer():
    parents = {4: {"author": "dc-andrew", "text": "can you deploy?\nplease", "origin": None}}
    line = event_line(reply_event(), parents)
    assert line.splitlines() == ["> `dc-andrew`: can you deploy? please", "**#fleet** `codex`: done, deployed"]


def test_no_quote_without_a_parent_or_without_a_destination():
    assert event_line(reply_event(), {}) == "**#fleet** `codex`: done, deployed"
    plain = reply_event()
    plain["payload"]["destination"] = None
    assert "> " not in event_line(plain, {4: {"author": "x", "text": "y"}})


def test_a_long_request_is_clipped_in_the_quote():
    parents = {4: {"author": "dc-andrew", "text": "x" * 500, "origin": None}}
    quote = event_line(reply_event(), parents).splitlines()[0]
    assert len(quote) < 160 and quote.endswith("…")
    [payload] = chat_payloads([reply_event()], "discord", parents)
    assert payload["allowed_mentions"] == {"parse": []}


# ---- addressing a destination -----------------------------------------


async def test_an_agent_addresses_one_hook_and_bypasses_its_filters(hooked):
    client, _app, sink = hooked
    await make_dest(client, "alerts", kinds=["task.updated"])  # messages are filtered out
    await make_dest(client, "other", kinds=["task.updated"])
    response = await send(client, "disk is almost full", destination="alerts")
    assert response.status_code == 201
    assert response.json()["message"]["destination"] == "alerts"
    assert await until(lambda: bodies_for(sink, "alerts"))
    await settle()
    assert "disk is almost full" in text_for(sink, "alerts")
    assert bodies_for(sink, "other") == []


async def test_a_directed_only_hook_ignores_everything_that_is_not_addressed_to_it(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", directed_only=True)
    await send(client, "ordinary chatter")
    task = (await client.post("/api/v1/tasks", json={"channel": "fleet", "title": "T", "actor": "codex"})).json()["task"]
    await client.post(f"/api/v1/tasks/{task['id']}/claim", json={"actor": "claude", "expected_version": task["version"]})
    await client.post("/api/v1/channels/fleet/state", json={"state": "review", "actor": "claude"})
    await settle()
    assert sink.requests == []
    await send(client, "need a human here", destination="help")
    assert await until(lambda: bodies_for(sink, "help"))
    await settle()
    assert len(bodies_for(sink, "help")) == 1
    assert "need a human here" in text_for(sink, "help")


async def test_a_hook_that_matches_by_filter_and_by_address_gets_one_copy(hooked):
    client, _app, sink = hooked
    await make_dest(client, "everything")
    await send(client, "just once", destination="everything")
    assert await until(lambda: bodies_for(sink, "everything"))
    await settle()
    assert text_for(sink, "everything").count("just once") == 1


async def test_a_bad_destination_is_refused_with_the_valid_names(hooked):
    client, _app, _sink = hooked
    await make_dest(client, "alerts")
    off = await make_dest(client, "paused")
    await client.patch(f"/api/v1/webhooks/{off['id']}", json={"enabled": False})
    await make_in(client)  # an inbound hook is not a destination
    missing = await send(client, "x", destination="nowhere")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "destination_not_found"
    assert missing.json()["error"]["details"]["destinations"] == ["alerts"]
    assert (await send(client, "x", destination="paused")).json()["error"]["code"] == "destination_disabled"
    assert (await send(client, "x", destination="inbox")).status_code == 404
    assert (await send(client, "x", destination="Bad Name")).status_code == 400
    assert (await client.get("/api/v1/messages", params={"channel": "fleet"})).json()["messages"] == []


async def test_destination_is_part_of_idempotency(hooked):
    client, _app, _sink = hooked
    await make_dest(client, "alerts")
    await make_dest(client, "other")
    key = {"client_request_id": "once-1"}
    first = await send(client, "same text", destination="alerts", **key)
    again = await send(client, "same text", destination="alerts", **key)
    assert first.json()["message"]["id"] == again.json()["message"]["id"]
    clash = await send(client, "same text", destination="other", **key)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_conflict"


async def test_list_destinations_shows_names_but_never_urls_or_secrets(hooked):
    client, _app, _sink = hooked
    created = await client.post(
        "/api/v1/webhooks",
        json={
            "name": "help",
            "direction": "out",
            "format": "discord",
            "url": url_for("TOKENSECRET"),
            "description": "urgent: a human must look",
            "directed_only": True,
            "discord_channel_id": DISCORD_CHANNEL,
        },
    )
    secret = created.json()["secret"]
    gone = await make_dest(client, "off")
    await client.patch(f"/api/v1/webhooks/{gone['id']}", json={"enabled": False})
    await make_in(client)
    listed = (await client.get("/api/v1/destinations")).json()
    assert listed["destinations"] == [
        {
            "name": "help",
            "description": "urgent: a human must look",
            "format": "discord",
            "discord_channel_id": DISCORD_CHANNEL,
            "directed_only": True,
        }
    ]
    via_mcp = await mcp_app.list_destinations()
    assert via_mcp == listed
    assert "TOKENSECRET" not in json.dumps([listed, via_mcp]) and secret not in json.dumps(listed)


async def test_mcp_post_message_takes_a_destination(hooked):
    _client, _app, sink = hooked
    await mcp_app.create_webhook("alerts", "out", url=url_for("alerts"), format="discord", kinds=["task.updated"])
    sent = await mcp_app.post_message("fleet", "builder", "from the mcp tool", destination="alerts")
    assert sent["message"]["destination"] == "alerts"
    assert await until(lambda: bodies_for(sink, "alerts"))
    assert "error" in await mcp_app.post_message("fleet", "builder", "x", destination="nope")


# ---- replying to a message that came from Discord ---------------------


async def test_a_reply_goes_back_to_the_discord_channel_the_request_came_from(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", discord_channel_id=DISCORD_CHANNEL, exclude_authors=["dc-*"])
    request = await discord_request(client, "@builder can you deploy?")
    assert request["origin"] == {
        "source": "discord",
        "channel_id": DISCORD_CHANNEL,
        "message_id": "1",
        "author": "andrew",
    }
    assert request["destination"] is None
    reply = await send(client, "done, deployed", reply_to=request["id"])
    assert reply.status_code == 201
    assert reply.json()["message"]["destination"] == "help"
    assert "warnings" not in reply.json()
    assert await until(lambda: bodies_for(sink, "help"))
    await settle()
    lines = text_for(sink, "help").splitlines()
    assert lines == ["> `dc-andrew`: @builder can you deploy?", "**#fleet** `builder`: done, deployed"]


async def test_a_reply_with_no_hook_for_that_discord_channel_stays_in_the_hub_and_warns(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", discord_channel_id="111", directed_only=True)
    request = await discord_request(client, "@builder hello", channel_id="222")
    reply = await send(client, "answer nobody will see", reply_to=request["id"])
    body = reply.json()
    assert body["message"]["destination"] is None
    assert "222" in body["warnings"][0] and "stays in the hub" in body["warnings"][0]
    await settle()
    assert sink.requests == []


async def test_an_explicit_destination_beats_the_automatic_one(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", discord_channel_id=DISCORD_CHANNEL, directed_only=True)
    await make_dest(client, "elsewhere", directed_only=True)
    request = await discord_request(client, "@builder hi")
    reply = await send(client, "routed by hand", reply_to=request["id"], destination="elsewhere")
    assert reply.json()["message"]["destination"] == "elsewhere"
    assert await until(lambda: bodies_for(sink, "elsewhere"))
    await settle()
    assert bodies_for(sink, "help") == []


async def test_a_reply_to_a_reply_still_finds_the_discord_channel(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", discord_channel_id=DISCORD_CHANNEL, directed_only=True)
    request = await discord_request(client, "@builder start")
    first = (await send(client, "working on it", reply_to=request["id"])).json()["message"]
    second = (await send(client, "finished", reply_to=first["id"])).json()["message"]
    assert second["destination"] == "help"
    assert await until(lambda: "finished" in text_for(sink, "help"))


async def test_a_reply_chain_longer_than_the_limit_is_not_routed(hooked):
    client, _app, _sink = hooked
    await make_dest(client, "help", discord_channel_id=DISCORD_CHANNEL, directed_only=True)
    parent = await discord_request(client, "@builder start")
    last = parent
    for index in range(REPLY_CHAIN_MAX):
        last = (await send(client, f"step {index}", reply_to=last["id"])).json()["message"]
    # The Discord message is now REPLY_CHAIN_MAX + 1 parents up.
    final = (await send(client, "too deep", reply_to=last["id"])).json()["message"]
    assert final["destination"] is None


async def test_a_plain_reply_to_a_hub_message_is_never_sent_to_discord(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", discord_channel_id=DISCORD_CHANNEL, directed_only=True)
    parent = (await send(client, "internal note")).json()["message"]
    reply = await send(client, "internal answer", reply_to=parent["id"])
    assert reply.json()["message"]["destination"] is None and "warnings" not in reply.json()
    await settle()
    assert sink.requests == []


async def test_origin_is_validated(hooked):
    client, _app, _sink = hooked

    async def attempt(origin):
        return await send(client, "x", origin=origin)

    assert (await attempt({"source": "slack", "channel_id": "1"})).status_code == 400
    assert (await attempt({"source": "discord", "channel_id": "abc"})).status_code == 400
    assert (await attempt({"source": "discord"})).status_code == 400
    assert (await attempt("discord")).status_code == 400
    assert (await attempt({"source": "discord", "channel_id": "1", "author": "a" * 200})).status_code == 400
    ok = await attempt({"source": "discord", "channel_id": 123, "junk": "dropped"})
    assert ok.json()["message"]["origin"] == {"source": "discord", "channel_id": "123"}


async def test_an_inbound_webhook_cannot_set_destination_or_origin(hooked):
    client, _app, sink = hooked
    await make_dest(client, "help", directed_only=True, discord_channel_id=DISCORD_CHANNEL)
    secret = (await make_in(client))["secret"]
    response = await client.post(
        "/hooks/in/inbox",
        json={
            "text": "sneaky",
            "destination": "help",
            "origin": {"source": "discord", "channel_id": DISCORD_CHANNEL},
        },
        headers=bearer(secret),
    )
    message = response.json()["message"]
    assert message["destination"] is None and message["origin"] is None
    await settle()
    assert sink.requests == []


async def test_origin_and_destination_show_up_when_reading_messages(hooked):
    client, _app, _sink = hooked
    await make_dest(client, "help", discord_channel_id=DISCORD_CHANNEL, directed_only=True)
    request = await discord_request(client, "@builder hi")
    await send(client, "hello back", reply_to=request["id"])
    page = (await client.get("/api/v1/messages", params={"channel": "fleet"})).json()["messages"]
    assert [(m["origin"] is not None, m["destination"]) for m in page] == [(True, None), (False, "help")]
    plain = await post_message(client, "no routing")
    assert plain["origin"] is None and plain["destination"] is None


# ---- linking a hook to its Discord channel ----------------------------


async def test_the_hub_looks_up_the_discord_channel_of_a_new_hook(hooked):
    client, _app, sink = hooked
    created = await make_dest(client, "help")
    assert created["discord_channel_id"] == sink.lookup_channel_id
    assert [str(r.url) for r in sink.lookups] == [url_for("help")]
    assert sink.requests == []  # a lookup is not a delivery


async def test_a_failed_lookup_does_not_block_creating_the_hook(hooked):
    client, _app, sink = hooked
    sink.lookup_status = 404
    created = await make_dest(client, "help")
    assert created["discord_channel_id"] is None
    sink.lookup_status = 200
    sink.fail_with = httpx.ConnectError("down")
    assert (await make_dest(client, "help2"))["discord_channel_id"] is None


async def test_no_lookup_for_other_formats_or_an_explicit_id(hooked):
    client, _app, sink = hooked
    slack = await client.post(
        "/api/v1/webhooks", json={"name": "s", "direction": "out", "format": "slack", "url": "https://hooks.test/s"}
    )
    explicit = await make_dest(client, "e", discord_channel_id="777")
    assert slack.json()["webhook"]["discord_channel_id"] is None
    assert explicit["discord_channel_id"] == "777"
    assert sink.lookups == []


async def test_changing_the_url_relinks_the_discord_channel(hooked):
    client, _app, sink = hooked
    created = await make_dest(client, "help")
    sink.lookup_channel_id = "888"
    moved = await client.patch(f"/api/v1/webhooks/{created['id']}", json={"url": url_for("help2")})
    assert moved.json()["webhook"]["discord_channel_id"] == "888"
    sink.lookup_status = 404  # the new URL cannot be resolved: do not keep a stale link
    lost = await client.patch(f"/api/v1/webhooks/{created['id']}", json={"url": url_for("help3")})
    assert lost.json()["webhook"]["discord_channel_id"] is None
    # An explicit id wins and triggers no lookup.
    calls = len(sink.lookups)
    set_by_hand = await client.patch(
        f"/api/v1/webhooks/{created['id']}", json={"url": url_for("help4"), "discord_channel_id": "999"}
    )
    assert set_by_hand.json()["webhook"]["discord_channel_id"] == "999" and len(sink.lookups) == calls


async def test_hooks_without_a_linked_channel_get_linked_at_startup(hooked):
    client, app, sink = hooked
    sink.lookup_status = 404  # the lookup fails when the hooks are created
    unlinked = await make_dest(client, "old")
    already = await make_dest(client, "linked", discord_channel_id="777")
    slack = (
        await client.post(
            "/api/v1/webhooks",
            json={"name": "chat", "direction": "out", "format": "slack", "url": "https://hooks.test/s"},
        )
    ).json()["webhook"]
    assert unlinked["discord_channel_id"] is None
    sink.lookup_status = 200
    sink.lookups.clear()
    await app.state.webhooks.link_discord_channels()
    hooks = {h["name"]: h for h in (await client.get("/api/v1/webhooks")).json()["webhooks"]}
    assert hooks["old"]["discord_channel_id"] == sink.lookup_channel_id
    assert hooks["linked"]["discord_channel_id"] == "777"  # left alone
    assert hooks["chat"]["discord_channel_id"] is None  # not a Discord hook
    assert [str(r.url) for r in sink.lookups] == [url_for("old")]
    assert slack["name"] == "chat"


async def test_a_failing_lookup_at_startup_leaves_the_hook_unlinked(hooked):
    client, app, sink = hooked
    sink.lookup_status = 404
    await make_dest(client, "old")
    await app.state.webhooks.link_discord_channels()
    assert (await client.get("/api/v1/destinations")).json()["destinations"][0]["discord_channel_id"] is None


async def test_description_and_directed_only_can_be_changed(hooked):
    client, _app, _sink = hooked
    created = await make_dest(client, "help")
    patched = await client.patch(
        f"/api/v1/webhooks/{created['id']}",
        json={"description": "  urgent  ", "directed_only": True, "discord_channel_id": ""},
    )
    body = patched.json()["webhook"]
    assert (body["description"], body["directed_only"], body["discord_channel_id"]) == ("urgent", True, None)
    assert (await client.patch(f"/api/v1/webhooks/{created['id']}", json={"discord_channel_id": "x1"})).status_code == 400
    inbound = (await make_in(client))["webhook"]
    assert "directed_only" not in inbound and "description" not in inbound
    bad = await client.patch(f"/api/v1/webhooks/{inbound['id']}", json={"directed_only": True})
    assert bad.status_code == 400


# ---- schema upgrade ---------------------------------------------------


async def test_a_database_from_before_routing_upgrades_in_place(tmp_path):
    path = tmp_path / "v4.sqlite"
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO messages (channel_id, author, text, created_at) VALUES (1, 'old', 'kept', 't')")
        for table, column in (
            ("messages", "destination"),
            ("messages", "origin_json"),
            ("webhooks", "description"),
            ("webhooks", "directed_only"),
            ("webhooks", "discord_channel_id"),
        ):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 5")
    db = Database(str(path))
    await db.open()
    await db.close()
    with sqlite3.connect(path) as conn:
        message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        hook_cols = {row[1] for row in conn.execute("PRAGMA table_info(webhooks)")}
        row = conn.execute("SELECT text, destination, origin_json FROM messages").fetchone()
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert {"destination", "origin_json"} <= message_cols
    assert {"description", "directed_only", "discord_channel_id"} <= hook_cols
    assert row == ("kept", None, None)
    assert versions == list(range(1, SCHEMA_VERSION + 1))
