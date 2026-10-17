"""Evolve commands: the chat grammar, the human allowlist, acks, the pause flag, schema v13."""

from __future__ import annotations

import sqlite3

import pytest
from fastmcp import Client

from cat_fleet_chat import evolve, mcp_app
from cat_fleet_chat.config import Settings, human_handles, load_settings
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.evolve import Command, parse_command
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.validate import event_kinds
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import event_line

from conftest import _open


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "evolve.sqlite"))
    await db.open()
    try:
        found = Hub(db, WaitHub())
        found.human_handles = frozenset({"alice", "dc-carol"})
        yield found
    finally:
        await db.close()


def _settings(tmp_path, handles: frozenset[str]) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token="test-token",
        dev_origins=("http://127.0.0.1:3001",),
        webhooks_enabled=False,
        human_handles=handles,
    )


@pytest.fixture
async def app_client(tmp_path):
    """REST client on an app whose allowlist is ``alice`` (via Settings, like the env)."""
    async for client, app in _open(_settings(tmp_path, human_handles("Alice, "))):
        client.headers["Authorization"] = "Bearer test-token"
        yield client, app


async def make(hub: Hub, **overrides) -> dict:
    args = {
        "author": "bot-1",
        "project": "demo",
        "title": "Cache the wheels",
        "summary": "Builds download the same wheels.",
        "weight": 3,
    }
    args.update(overrides)
    return (await hub.create_proposal(**args))["proposal"]


async def messages(hub: Hub, channel: str = "fleet") -> list[dict]:
    return (await hub.get_messages(channel, limit=200))["messages"]


async def acks(hub: Hub, channel: str = "fleet") -> list[dict]:
    return [m for m in await messages(hub, channel) if m["author"] == evolve.ACK_AUTHOR]


# ---- parser -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("approve P12", Command("approve", number=12)),
        ("approve 12", Command("approve", number=12)),
        ("approve p3", Command("approve", number=3)),
        ("APPROVE P1", Command("approve", number=1)),
        ("/approve P1", Command("approve", number=1)),
        ("!Approve P1", Command("approve", number=1)),
        ("   approve P1   ", Command("approve", number=1)),
        ("\n\n  approve P1\nthanks, ship it", Command("approve", number=1)),
        ("reject P2 out of scope", Command("reject", number=2, reason="out of scope")),
        ("Reject 2   too big for now ", Command("reject", number=2, reason="too big for now")),
        ("edit P3 title New title", Command("edit", number=3, field="title", value="New title")),
        ("/edit P3 weight 5", Command("edit", number=3, field="weight", value="5")),
        ("edit P3 Summary Two words", Command("edit", number=3, field="summary", value="Two words")),
        ("edit P3 pr_ref #42", Command("edit", number=3, field="pr_ref", value="#42")),
        ("pause evolve", Command("pause")),
        ("PAUSE Evolve", Command("pause")),
        ("!resume evolve", Command("resume")),
    ],
)
def test_parse_valid_commands(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n  ",
        None,
        42,
        "I think we should approve P12",
        "please approve P12",
        "approve",
        "approve P12 now",
        "approve P0",
        "approve P-1",
        "approve PP1",
        "approve P²",
        "approved P12",
        "approveP12",
        "//approve P12",
        "? approve P12",
        "hello\napprove P12",
        # A reason is required to reject.
        "reject P2",
        "reject P2   ",
        "reject",
        # Fields outside proposals.EDITABLE_FIELDS are refused.
        "edit P3 status accepted",
        "edit P3 author mallory",
        "edit P3 decided_by alice",
        "edit P3 title",
        "edit title New",
        "pause",
        "pause evolve now",
        "pause the evolve",
        "resume",
        "stop evolve",
        # Acks are never commands.
        "approved P12",
        "rejected P12: too big",
        "P12 title updated",
        "evolve paused",
        "P99 not found",
    ],
)
def test_parse_non_commands_return_none(text):
    assert parse_command(text) is None


def test_human_handles_from_env(monkeypatch):
    monkeypatch.setenv("CAT_FLEET_HUMAN_HANDLES", " Alice , bob,, ")
    assert load_settings().human_handles == frozenset({"alice", "bob"})
    monkeypatch.setenv("CAT_FLEET_HUMAN_HANDLES", "")
    assert load_settings().human_handles == frozenset()
    monkeypatch.delenv("CAT_FLEET_HUMAN_HANDLES")
    assert load_settings().human_handles == frozenset()


# ---- hub ------------------------------------------------------------------------


async def test_allowlisted_approve_accepts_and_acks(hub):
    proposal = await make(hub)
    posted = (await hub.post_message("fleet", "alice", f"approve {proposal['id']}"))["message"]
    found = (await hub.get_proposal("P1"))["proposal"]
    assert found["status"] == "accepted" and found["decided_by"] == "alice"
    [ack] = await acks(hub)
    assert ack["text"] == "approved P1"
    assert ack["reply_to"] == posted["id"]
    # The ack is the last message: it was not parsed again.
    assert [m["author"] for m in await messages(hub)][-2:] == ["alice", "evolve"]


async def test_allowlist_is_case_insensitive(hub):
    await make(hub)
    await hub.post_message("fleet", "ALICE", "/APPROVE p1")
    found = (await hub.get_proposal("P1"))["proposal"]
    assert found["status"] == "accepted" and found["decided_by"] == "ALICE"


async def test_reject_sets_status_and_reason(hub):
    await make(hub)
    await hub.post_message("fleet", "alice", "reject P1 not worth it this week")
    found = (await hub.get_proposal("P1"))["proposal"]
    assert (found["status"], found["reason"], found["decided_by"]) == (
        "rejected",
        "not worth it this week",
        "alice",
    )
    assert [a["text"] for a in await acks(hub)] == ["rejected P1: not worth it this week"]


async def test_edit_updates_field(hub):
    await make(hub)
    await hub.post_message("fleet", "alice", "edit P1 title New title here")
    await hub.post_message("fleet", "alice", "edit P1 weight 7")
    found = (await hub.get_proposal("P1"))["proposal"]
    assert found["title"] == "New title here" and found["weight"] == 7
    assert found["status"] == "open"
    assert [a["text"] for a in await acks(hub)] == ["P1 title updated", "P1 weight updated"]


async def test_reject_without_reason_is_an_ordinary_message(hub):
    await make(hub)
    await hub.post_message("fleet", "alice", "reject P1")
    await hub.post_message("fleet", "alice", "edit P1 status accepted")
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "open"
    assert await acks(hub) == []


async def test_non_allowlisted_handle_is_ignored(hub):
    await make(hub)
    await hub.post_message("fleet", "bot-1", "approve P1")
    await hub.post_message("fleet", "mallory", "pause evolve")
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "open"
    assert (await hub.evolve_state())["paused"] is False
    assert await acks(hub) == []
    assert [m["text"] for m in await messages(hub)] == ["approve P1", "pause evolve"]


async def test_empty_allowlist_ignores_everyone(hub):
    hub.human_handles = frozenset()
    await make(hub)
    await hub.post_message("fleet", "alice", "approve P1")
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "open"
    assert await acks(hub) == []


async def test_errors_are_acks_not_exceptions(hub):
    await make(hub)
    await hub.post_message("fleet", "alice", "approve P99")
    await hub.post_message("fleet", "alice", "approve P1")
    # accepted -> accepted is not a valid transition.
    second = await hub.post_message("fleet", "alice", "approve P1")
    assert second["message"]["text"] == "approve P1"
    await hub.post_message("fleet", "alice", "edit P1 weight 99")
    texts = [a["text"] for a in await acks(hub)]
    assert texts[0] == "P99 not found"
    assert texts[1] == "approved P1"
    assert texts[2].startswith("approve P1 failed: proposal is already accepted")
    assert texts[3].startswith("edit P1 failed: weight must be an integer")
    assert (await hub.get_proposal("P1"))["proposal"]["weight"] == 3


async def test_apply_command_returns_error_dict(hub):
    outcome = await hub.apply_command(Command("reject", number=5, reason="no"), "alice")
    assert outcome == {"command": "reject", "ok": False, "error": "not_found", "text": "P5 not found"}


async def test_ack_from_allowlisted_evolve_author_is_not_parsed(hub):
    hub.human_handles = frozenset({"alice", "evolve"})
    await make(hub)
    await hub.post_message("fleet", "evolve", "approve P1")
    assert (await hub.get_proposal("P1"))["proposal"]["status"] == "open"
    assert len(await messages(hub)) == 1


async def test_idempotent_retry_does_not_rerun_the_command(hub):
    await make(hub)
    await hub.post_message("fleet", "alice", "pause evolve", client_request_id_value="req-1")
    await hub.post_message("fleet", "alice", "pause evolve", client_request_id_value="req-1")
    assert [a["text"] for a in await acks(hub)] == ["evolve paused"]


async def test_ack_goes_to_the_command_channel(hub):
    await hub.create_channel("ops")
    await make(hub)
    await hub.post_message("ops", "alice", "approve P1")
    assert [a["text"] for a in await acks(hub, "ops")] == ["approved P1"]
    assert await acks(hub, "fleet") == []


async def test_pause_and_resume_are_idempotent_and_emit_events(hub):
    assert await hub.evolve_state() == {"paused": False, "by": None, "at": None}
    await hub.post_message("fleet", "alice", "pause evolve")
    state = await hub.evolve_state()
    assert state["paused"] is True and state["by"] == "alice" and state["at"]
    await hub.post_message("fleet", "alice", "pause evolve")
    await hub.post_message("fleet", "alice", "resume evolve")
    await hub.post_message("fleet", "alice", "resume evolve")
    assert await hub.evolve_state() == {"paused": False, "by": None, "at": None}
    assert [a["text"] for a in await acks(hub)] == [
        "evolve paused",
        "evolve already paused",
        "evolve resumed",
        "evolve already resumed",
    ]
    events = [
        e for e in await hub.events_after(0, channel=None, limit=1_000) if e["kind"].startswith("evolve_")
    ]
    assert [e["kind"] for e in events] == ["evolve_paused", "evolve_resumed"]
    assert events[0]["payload"]["actor"] == "alice" and events[0]["channel"] is None
    assert event_kinds("evolve_paused,evolve_resumed") == {"evolve_paused", "evolve_resumed"}
    assert event_line(events[0]) == "⏸️ evolve paused by `alice`"


# ---- REST and inbound hooks ---------------------------------------------------


async def test_rest_post_commands_and_evolve_state(app_client):
    client, app = app_client
    await make(app.state.hub)
    assert (await client.get("/api/v1/evolve/state")).json() == {"paused": False, "by": None, "at": None}
    for text in ("approve P1", "pause evolve"):
        response = await client.post(
            "/api/v1/messages", json={"channel": "fleet", "author": "alice", "text": text}
        )
        assert response.status_code == 201
    state = (await client.get("/api/v1/evolve/state")).json()
    assert state["paused"] is True and state["by"] == "alice"
    assert (await app.state.hub.get_proposal("P1"))["proposal"]["status"] == "accepted"
    await client.post("/api/v1/messages", json={"channel": "fleet", "author": "alice", "text": "resume evolve"})
    assert (await client.get("/api/v1/evolve/state")).json()["paused"] is False
    # No REST write for the flag.
    assert (await client.post("/api/v1/evolve/state", json={"paused": True})).status_code == 405


async def test_rest_evolve_state_needs_the_token(app_client):
    client, _app = app_client
    response = await client.get("/api/v1/evolve/state", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def _inbound(client, name: str, **extra) -> str:
    body = {"name": name, "direction": "in", "channel": "fleet", **extra}
    response = await client.post("/api/v1/webhooks", json=body)
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_inbound_hook_commands(app_client):
    client, app = app_client
    hub = app.state.hub
    await make(hub)
    await make(hub, title="Second")
    # The hook's fixed author is the sender.
    secret = await _inbound(client, "alice-in", author="alice")
    response = await client.post(
        "/hooks/in/alice-in", json={"text": "approve P1"}, headers={"Authorization": f"Bearer {secret}"}
    )
    assert response.status_code == 201
    assert (await hub.get_proposal("P1"))["proposal"]["decided_by"] == "alice"
    # A relay-style hook carries a per-message author.
    relay = await _inbound(client, "relay", allow_override=True)
    headers = {"Authorization": f"Bearer {relay}"}
    await client.post("/hooks/in/relay", json={"author": "mallory", "text": "approve P2"}, headers=headers)
    assert (await hub.get_proposal("P2"))["proposal"]["status"] == "open"
    await client.post("/hooks/in/relay", json={"author": "Alice", "text": "reject P2 dup of P1"}, headers=headers)
    found = (await hub.get_proposal("P2"))["proposal"]
    assert (found["status"], found["reason"]) == ("rejected", "dup of P1")
    assert [a["text"] for a in await acks(hub)] == ["approved P1", "rejected P2: dup of P1"]


# ---- MCP --------------------------------------------------------------------------


async def test_mcp_get_evolve_state(hub):
    mcp_app.set_hub(hub)
    try:
        async with Client(mcp_app.build_mcp()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert "get_evolve_state" in names
            assert not any("pause" in name for name in names)
            state = (await client.call_tool("get_evolve_state", {})).structured_content
            assert state == {"paused": False, "by": None, "at": None}
            posted = await client.call_tool(
                "post_message", {"channel": "fleet", "author": "alice", "text": "pause evolve"}
            )
            assert posted.structured_content["message"]["author"] == "alice"
            state = (await client.call_tool("get_evolve_state", {})).structured_content
            assert state["paused"] is True and state["by"] == "alice"
    finally:
        mcp_app.set_hub(None)


# ---- schema -----------------------------------------------------------------------


async def test_v12_database_migrates_to_v13(tmp_path):
    assert SCHEMA_VERSION >= 13
    path = tmp_path / "v12.sqlite"
    db = Database(str(path))
    await db.open()
    old = Hub(db, WaitHub())
    await old.post_message("fleet", "alice", "kept message")
    await make(old)
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE evolve_state")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 13")

    for _ in range(2):
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        columns = [r[1] for r in conn.execute("PRAGMA table_info(evolve_state)")]
        texts = [r[0] for r in conn.execute("SELECT text FROM messages")]
        titles = [r[0] for r in conn.execute("SELECT title FROM proposals")]
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert columns == ["id", "paused", "paused_by", "paused_at", "updated_at"]
    assert texts == ["kept message"] and titles == ["Cache the wheels"]

    db = Database(str(path))
    await db.open()
    try:
        fresh = Hub(db, WaitHub())
        assert await fresh.evolve_state() == {"paused": False, "by": None, "at": None}
        await fresh.set_evolve_paused(True, "alice")
        assert (await fresh.evolve_state())["paused"] is True
    finally:
        await db.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evolve_state").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO evolve_state (id, paused, updated_at) VALUES (2, 0, 'x')")
