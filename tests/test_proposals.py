"""Proposals: create/list/get/update over hub, REST and MCP; status machine; feed root post; events; schema v11."""

from __future__ import annotations

import sqlite3

import pytest
from fastmcp import Client

from cat_fleet_chat import mcp_app, proposals
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.validate import event_kinds
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import chat_payloads, event_line, matches


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "proposals.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


async def make(hub: Hub, **overrides):
    args = {
        "author": "ada",
        "project": "demo",
        "title": "Cache the wheels",
        "summary": "Builds spend minutes downloading the same wheels.",
        "weight": 3,
    }
    args.update(overrides)
    return (await hub.create_proposal(**args))["proposal"]


async def all_events(hub: Hub) -> list[dict]:
    return await hub.events_after(0, channel=None, limit=1_000)


async def walk(hub: Hub, number: int, *statuses: str) -> dict:
    found = None
    for status in statuses:
        changes = {"status": status}
        if status == "rejected":
            changes["reason"] = "not now"
        found = (await hub.update_proposal(number, "bob", changes))["proposal"]
        assert found["status"] == status
    return found


# ---- pure helpers -----------------------------------------------------------


def test_schema_version_includes_v11():
    # v11 added proposals; later steps may bump the version further (v12 reactions).
    assert SCHEMA_VERSION >= 11


def test_proposal_number_accepts_p_prefix_and_plain_ids():
    assert proposals.proposal_number("P12") == 12
    assert proposals.proposal_number("p7") == 7
    assert proposals.proposal_number("12") == 12
    assert proposals.proposal_number(12) == 12
    for bad in ("P0", "0", "P-1", "PP1", "", None, True, 1.5, "P1x"):
        with pytest.raises(HubError) as info:
            proposals.proposal_number(bad)
        assert info.value.code == "validation_error"


def test_field_validators():
    assert proposals.weight_value(1) == 1 and proposals.weight_value("10") == 10
    assert proposals.weight_value(" 7 ") == 7
    for bad in (0, 11, "x", True, 2.5, None, "--1", "-", "-3", "+5", "1_0", "٣", "²", ""):
        with pytest.raises(HubError):
            proposals.weight_value(bad)
    assert proposals.title_text("  two\n words ") == "two words"
    with pytest.raises(HubError):
        proposals.title_text("x" * (proposals.TITLE_MAX + 1))
    assert proposals.evidence_list(None) == []
    assert proposals.evidence_list(["https://example.test/a", "post 4"]) == ["https://example.test/a", "post 4"]
    for bad in ([""], [1], ["x"] * (proposals.EVIDENCE_MAX + 1), {"a": 1}):
        with pytest.raises(HubError):
            proposals.evidence_list(bad)
    with pytest.raises(HubError):
        proposals.ref_text("a\nb", "pr_ref")
    assert proposals.ref_text("", "pr_ref") is None


def test_editable_field_list_is_one_constant():
    assert proposals.EDITABLE_FIELDS == ("title", "summary", "evidence", "weight", "run_ref", "step_ref", "pr_ref")
    with pytest.raises(HubError):
        proposals.clean_field("status", "open")


# ---- hub ----------------------------------------------------------------------


async def test_create_writes_proposal_and_feed_root_post(hub):
    created = await make(hub, evidence=["https://example.test/run/1"])
    assert created["id"] == "P1" and created["number"] == 1
    assert created["status"] == "open" and created["weight"] == 3
    assert created["evidence"] == ["https://example.test/run/1"]
    assert created["decided_by"] is None and created["pr_ref"] is None

    post = (await hub.feed_get_post(created["post_id"]))["post"]
    assert post["kind"] == "post" and post["root_id"] == post["id"]
    assert post["author"] == "ada" and "proposal" in post["hashtags"]
    assert post["body"].startswith("P1: Cache the wheels")
    assert "Builds spend minutes" in post["body"]

    tagged = await hub.feed_timeline(scope="hashtag", tag="proposal")
    assert [p["id"] for p in tagged["posts"]] == [created["post_id"]]
    # Discussion is a normal reply to the root post.
    reply = await hub.feed_post("bob", "+1, saw this too", parent_id=created["post_id"])
    assert reply["post"]["root_id"] == created["post_id"]


async def test_create_validation(hub):
    cases = [
        {"project": "Not A Slug!"},
        {"title": " "},
        {"summary": ""},
        {"weight": 0},
        {"weight": 11},
        {"weight": "--1"},
        {"evidence": [""]},
        {"author": "bad handle"},
    ]
    for override in cases:
        with pytest.raises(HubError) as info:
            await make(hub, **override)
        assert info.value.code == "validation_error", override
    assert (await hub.list_proposals())["proposals"] == []
    assert (await hub.feed_timeline())["posts"] == []


async def test_create_is_idempotent_with_client_request_id(hub):
    first = await make(hub, client_request_id="req-1")
    again = await make(hub, client_request_id="req-1")
    assert again == first
    with pytest.raises(HubError) as info:
        await make(hub, client_request_id="req-1", title="Something else")
    assert info.value.code == "idempotency_conflict"
    assert len((await hub.list_proposals())["proposals"]) == 1


async def test_get_accepts_both_id_forms_and_404s(hub):
    created = await make(hub)
    assert (await hub.get_proposal("P1"))["proposal"] == created
    assert (await hub.get_proposal(1))["proposal"] == created
    with pytest.raises(HubError) as info:
        await hub.get_proposal("P99")
    assert info.value.status == 404


async def test_list_filters_and_paging(hub):
    a = await make(hub, project="demo", title="A")
    b = await make(hub, project="other", title="B")
    c = await make(hub, project="demo", title="C")
    await walk(hub, c["number"], "accepted")

    assert [p["id"] for p in (await hub.list_proposals())["proposals"]] == [c["id"], b["id"], a["id"]]
    assert [p["id"] for p in (await hub.list_proposals(project="demo"))["proposals"]] == [c["id"], a["id"]]
    assert [p["id"] for p in (await hub.list_proposals(status="open"))["proposals"]] == [b["id"], a["id"]]
    assert [p["id"] for p in (await hub.list_proposals(project="demo", status="accepted"))["proposals"]] == [c["id"]]

    page = await hub.list_proposals(limit=2)
    assert page["has_more"] is True and len(page["proposals"]) == 2
    rest = await hub.list_proposals(limit=2, cursor=page["next_cursor"])
    assert [p["id"] for p in rest["proposals"]] == [a["id"]] and rest["has_more"] is False

    with pytest.raises(HubError):
        await hub.list_proposals(status="maybe")


async def test_every_allowed_transition_succeeds(hub):
    for source, targets in proposals.TRANSITIONS.items():
        for target in targets:
            created = await make(hub, title=f"{source} to {target}")
            path = {
                "open": [],
                "accepted": ["accepted"],
                "building": ["accepted", "building"],
                "rejected": ["rejected"],
            }[source]
            await walk(hub, created["number"], *path)
            changes = {"status": target}
            if target == "rejected":
                changes["reason"] = "duplicate of P1"
            result = await hub.update_proposal(created["id"], "carol", changes)
            assert result["previous_status"] == source
            assert result["proposal"]["status"] == target


async def test_invalid_transitions_are_refused_with_allowed_next_statuses(hub):
    built = await walk(hub, (await make(hub))["number"], "accepted", "building", "built")
    rejected = await walk(hub, (await make(hub))["number"], "rejected")
    opened = await make(hub)
    withdrawn = await walk(hub, (await make(hub))["number"], "withdrawn")
    cases = [
        (opened, "built", ["accepted", "rejected", "withdrawn"]),
        (opened, "building", ["accepted", "rejected", "withdrawn"]),
        (opened, "open", ["accepted", "rejected", "withdrawn"]),
        (built, "open", []),
        (built, "built", []),
        (rejected, "building", ["open"]),
        (rejected, "accepted", ["open"]),
        (withdrawn, "open", []),
    ]
    for proposal, target, allowed in cases:
        with pytest.raises(HubError) as info:
            await hub.update_proposal(proposal["id"], "bob", {"status": target})
        error = info.value
        assert error.code == "invalid_transition" and error.status == 409, (proposal["status"], target)
        assert error.details["allowed"] == allowed
        for name in allowed:
            assert name in error.message
        assert (await hub.get_proposal(proposal["id"]))["proposal"]["status"] == proposal["status"]


async def test_reject_requires_reason_and_records_decision(hub):
    created = await make(hub)
    for reason in (None, "", "   "):
        changes = {"status": "rejected"} if reason is None else {"status": "rejected", "reason": reason}
        with pytest.raises(HubError) as info:
            await hub.update_proposal(created["id"], "bob", changes)
        assert info.value.code == "validation_error" and info.value.details["field"] == "reason"
    rejected = (await hub.update_proposal(created["id"], "bob", {"status": "rejected", "reason": "out of scope"}))["proposal"]
    assert rejected["decided_by"] == "bob" and rejected["decided_at"] and rejected["reason"] == "out of scope"
    reopened = (await hub.update_proposal(created["id"], "ada", {"status": "open"}))["proposal"]
    assert reopened["decided_by"] is None and reopened["decided_at"] is None and reopened["reason"] is None
    accepted = (await hub.update_proposal(created["id"], "carol", {"status": "accepted"}))["proposal"]
    assert accepted["decided_by"] == "carol" and accepted["reason"] is None
    withdrawn = (await hub.update_proposal(created["id"], "ada", {"status": "withdrawn", "reason": "superseded"}))["proposal"]
    assert withdrawn["decided_by"] == "ada" and withdrawn["reason"] == "superseded"


async def test_update_edits_fields_while_open_or_accepted(hub):
    created = await make(hub)
    edited = (
        await hub.update_proposal(
            "P1",
            "ada",
            {"title": "Cache wheels and sdists", "summary": "New summary", "evidence": ["post 9"], "weight": 5},
        )
    )["proposal"]
    assert edited["title"] == "Cache wheels and sdists" and edited["summary"] == "New summary"
    assert edited["evidence"] == ["post 9"] and edited["weight"] == 5 and edited["status"] == "open"
    assert edited["updated_at"] >= created["updated_at"]

    await walk(hub, 1, "accepted")
    linked = (await hub.update_proposal("P1", "bob", {"status": "building", "run_ref": "run-1", "step_ref": "S01"}))["proposal"]
    assert linked["status"] == "building" and linked["run_ref"] == "run-1" and linked["step_ref"] == "S01"
    with pytest.raises(HubError) as info:
        await hub.update_proposal("P1", "bob", {"title": "late change"})
    assert info.value.code == "proposal_locked" and info.value.details["allowed"] == list(proposals.REF_FIELDS)
    built = (await hub.update_proposal("P1", "bob", {"status": "built", "pr_ref": "https://example.test/pr/3"}))["proposal"]
    assert built["pr_ref"] == "https://example.test/pr/3"
    cleared = (await hub.update_proposal("P1", "bob", {"run_ref": ""}))["proposal"]
    assert cleared["run_ref"] is None

    other = await make(hub)
    await walk(hub, other["number"], "withdrawn")
    with pytest.raises(HubError) as info:
        await hub.update_proposal(other["id"], "ada", {"weight": 2})
    assert info.value.code == "proposal_locked"


async def test_update_rejects_bad_changes(hub):
    await make(hub)
    for changes in ({}, {"author": "eve"}, {"reason": "why"}, {"weight": 99}, {"status": "nope"}, "x"):
        with pytest.raises(HubError) as info:
            await hub.update_proposal("P1", "ada", changes)
        assert info.value.code == "validation_error", changes
    with pytest.raises(HubError) as info:
        await hub.update_proposal("P42", "ada", {"weight": 2})
    assert info.value.status == 404
    same = await hub.update_proposal("P1", "ada", {"weight": 3})
    assert same["idempotent"] is True


async def test_events_emitted_for_create_and_update(hub):
    created = await make(hub)
    await hub.update_proposal(created["id"], "bob", {"weight": 4})
    await hub.update_proposal(created["id"], "bob", {"status": "rejected", "reason": "not this week"})
    await hub.update_proposal(created["id"], "bob", {"weight": 4, "status": "open"})  # no-op weight, status changes
    events = await all_events(hub)
    kinds = [event["kind"] for event in events]
    assert kinds.count("feed_post") == 1
    found = [event for event in events if event["kind"].startswith("proposal_")]
    assert [event["kind"] for event in found] == [
        "proposal_created",
        "proposal_updated",
        "proposal_updated",
        "proposal_updated",
    ]
    assert all(event["channel"] is None and event["entity_id"] == 1 for event in found)
    first, edit, reject, reopen = (event["payload"] for event in found)
    assert first["id"] == "P1" and first["status"] == "open" and first["post_id"] == created["post_id"]
    assert first["actor"] == "ada" and first["project"] == "demo"
    assert edit["changed"] == ["weight"] and "previous_status" not in edit
    assert reject["previous_status"] == "open" and reject["status"] == "rejected"
    assert reject["reason"] == "not this week" and reject["actor"] == "bob"
    assert reopen["previous_status"] == "rejected" and reopen["changed"] == []

    for event in found:
        assert event["payload"]["id"] in event_line(event)
    assert "open → rejected" in event_line(found[2])
    assert chat_payloads(found, "discord") and chat_payloads(found, "slack")
    hook = {"name": "h", "kinds": [], "channels": [], "mentions": [], "exclude_authors": []}
    assert matches(hook, found[0]) is True
    assert matches({**hook, "kinds": ["proposal_updated"]}, found[0]) is False
    assert matches({**hook, "exclude_authors": ["bo*"]}, found[2]) is False


async def test_failed_create_writes_nothing(hub, monkeypatch):
    from cat_fleet_chat import store

    async def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(store, "update_proposal", boom)
    with pytest.raises(RuntimeError):
        await make(hub)
    monkeypatch.undo()
    assert (await hub.list_proposals())["proposals"] == []
    assert (await hub.feed_timeline())["posts"] == []
    assert await all_events(hub) == []


# ---- REST ---------------------------------------------------------------------


async def test_rest_round_trip(running):
    client, _ = running
    created = await client.post(
        "/api/v1/proposals",
        json={
            "agent_name": "ada",
            "project": "demo",
            "title": "Cache the wheels",
            "summary": "Downloads are slow.",
            "weight": 2,
            "evidence": ["https://example.test/log/1"],
        },
    )
    assert created.status_code == 201, created.text
    proposal = created.json()["proposal"]
    assert proposal["id"] == "P1" and proposal["author"] == "ada"
    await client.post(
        "/api/v1/proposals",
        json={"author": "bob", "project": "other", "title": "B", "summary": "b", "weight": 1},
    )

    for ident in ("P1", "1", "p1"):
        got = await client.get(f"/api/v1/proposals/{ident}")
        assert got.status_code == 200 and got.json()["proposal"]["id"] == "P1"
    assert (await client.get("/api/v1/proposals/P9")).status_code == 404
    assert (await client.get("/api/v1/proposals/banana")).status_code == 400

    listed = (await client.get("/api/v1/proposals")).json()
    assert [p["id"] for p in listed["proposals"]] == ["P2", "P1"]
    demo = (await client.get("/api/v1/proposals", params={"project": "demo"})).json()
    assert [p["id"] for p in demo["proposals"]] == ["P1"]

    post = (await client.get(f"/api/v1/feed/posts/{proposal['post_id']}")).json()["post"]
    assert "proposal" in post["hashtags"] and post["body"].startswith("P1:")

    patched = await client.patch("/api/v1/proposals/P1", json={"actor": "carol", "status": "accepted", "weight": 4})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["proposal"]["status"] == "accepted" and body["proposal"]["weight"] == 4
    assert body["proposal"]["decided_by"] == "carol" and body["previous_status"] == "open"
    accepted = (await client.get("/api/v1/proposals", params={"status": "accepted"})).json()
    assert [p["id"] for p in accepted["proposals"]] == ["P1"]

    bad = await client.patch("/api/v1/proposals/P1", json={"actor": "carol", "status": "built"})
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "invalid_transition"
    assert bad.json()["error"]["details"]["allowed"] == ["building", "rejected", "withdrawn"]
    no_reason = await client.patch("/api/v1/proposals/P1", json={"actor": "carol", "status": "rejected"})
    assert no_reason.status_code == 400
    no_actor = await client.patch("/api/v1/proposals/P1", json={"weight": 5})
    assert no_actor.status_code == 400 and no_actor.json()["error"]["details"]["field"] == "actor"
    for weight in ("--1", "²"):
        bad_weight = await client.patch("/api/v1/proposals/P1", json={"actor": "carol", "weight": weight})
        assert bad_weight.status_code == 400 and bad_weight.json()["error"]["details"]["field"] == "weight"


def test_proposal_event_kinds_are_filterable():
    # /events and webhooks validate ``kinds`` with this; the SSE log carries the kinds.
    assert event_kinds("proposal_created,proposal_updated") == frozenset({"proposal_created", "proposal_updated"})


async def test_rest_proposals_require_auth(authed):
    client, _ = authed
    for method, path in (
        ("GET", "/api/v1/proposals"),
        ("POST", "/api/v1/proposals"),
        ("GET", "/api/v1/proposals/P1"),
        ("PATCH", "/api/v1/proposals/P1"),
    ):
        response = await client.request(method, path, json={})
        assert response.status_code == 401, (method, path)
    ok = await client.get("/api/v1/proposals", headers={"Authorization": "Bearer test-token"})
    assert ok.status_code == 200 and ok.json() == {"proposals": [], "next_cursor": None, "has_more": False}


# ---- MCP ----------------------------------------------------------------------


async def test_mcp_proposal_tools(hub):
    mcp_app.set_hub(hub)
    try:
        async with Client(mcp_app.build_mcp()) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"create_proposal", "list_proposals", "get_proposal", "update_proposal"} <= names
            created = (
                await client.call_tool(
                    "create_proposal",
                    {
                        "author": "ada",
                        "project": "demo",
                        "title": "Retry flaky uploads",
                        "summary": "Uploads fail once a day.",
                        "weight": 2,
                        "evidence": ["post 3"],
                    },
                )
            ).structured_content
            assert created["proposal"]["id"] == "P1"
            got = (await client.call_tool("get_proposal", {"proposal_id": "P1"})).structured_content
            assert got["proposal"]["title"] == "Retry flaky uploads"
            got_int = (await client.call_tool("get_proposal", {"proposal_id": 1})).structured_content
            assert got_int["proposal"]["id"] == "P1"
            listed = (await client.call_tool("list_proposals", {"project": "demo", "status": "open"})).structured_content
            assert [p["id"] for p in listed["proposals"]] == ["P1"]
            updated = (
                await client.call_tool(
                    "update_proposal", {"proposal_id": "P1", "actor": "bob", "status": "accepted", "weight": 3}
                )
            ).structured_content
            assert updated["proposal"]["status"] == "accepted" and updated["proposal"]["weight"] == 3
            error = (
                await client.call_tool("update_proposal", {"proposal_id": "P1", "actor": "bob", "status": "open"})
            ).structured_content
            assert error["status"] == "error" and error["error"] == "invalid_transition"
            assert error["details"]["allowed"] == ["building", "rejected", "withdrawn"]
            missing = (await client.call_tool("get_proposal", {"proposal_id": "P7"})).structured_content
            assert missing["status"] == "error" and missing["http_status"] == 404
    finally:
        mcp_app.set_hub(None)


# ---- schema -------------------------------------------------------------------


async def test_v10_database_migrates_to_v11(tmp_path):
    path = tmp_path / "v10.sqlite"
    db = Database(str(path))
    await db.open()
    old = Hub(db, WaitHub())
    await old.post_message("fleet", "ada", "kept message")
    await old.feed_post("ada", "kept post #demo")
    await old.knowledge_add("demo", "Kept fact", author="ada")
    await db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE proposals")
        conn.execute("DROP TABLE IF EXISTS reactions")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 11")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "proposals" not in tables

    for _ in range(2):
        db = Database(str(path))
        await db.open()
        await db.close()

    with sqlite3.connect(path) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        columns = [r[1] for r in conn.execute("PRAGMA table_info(proposals)")]
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        messages = [r[0] for r in conn.execute("SELECT text FROM messages")]
        posts = [r[0] for r in conn.execute("SELECT body FROM feed_posts")]
        facts = [r[0] for r in conn.execute("SELECT fact FROM knowledge_facts")]
    assert versions == list(range(1, SCHEMA_VERSION + 1))
    assert columns == [
        "id", "project", "title", "summary", "evidence", "weight", "status", "author",
        "decided_by", "decided_at", "reason", "run_ref", "step_ref", "pr_ref", "post_id",
        "created_at", "updated_at",
    ]
    assert {"idx_proposals_project", "idx_proposals_status", "idx_proposals_post"} <= indexes
    assert messages == ["kept message"] and posts == ["kept post #demo"] and facts == ["Kept fact"]

    db = Database(str(path))
    await db.open()
    try:
        hub = Hub(db, WaitHub())
        created = await make(hub)
        assert created["id"] == "P1"
        assert len((await hub.feed_timeline())["posts"]) == 2
    finally:
        await db.close()
