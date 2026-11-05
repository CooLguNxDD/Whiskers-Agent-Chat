"""Proposals render with one fixed template: Discord embeds (one per message) and relay cards."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from cat_fleet_chat import discord_relay as relay
from cat_fleet_chat.db import Database
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.relay.render import render
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.webhooks import chat_payloads, discord_embed


@pytest.fixture
async def hub(tmp_path):
    db = Database(str(tmp_path / "hub.sqlite"))
    await db.open()
    try:
        yield Hub(db, WaitHub())
    finally:
        await db.close()


def proposal_event(number=142, *, id=10, status="open", evidence=None, **extra):
    payload = {
        "id": f"P{number}",
        "number": number,
        "project": "worldgen-v3",
        "title": "Re-measure gate-budget units",
        "summary": "Budgets rose because concurrent gates slowed the host.",
        "evidence": evidence if evidence is not None else ["testdata/gate-budget.json", "research-2026-11-03.log"],
        "weight": 2,
        "status": status,
        "author": "cat-fleet",
        "actor": "cat-fleet",
        **extra,
    }
    return {"id": id, "kind": "proposal_created", "channel": None, "payload": payload}


def msg_event(id=1, text="hi"):
    return {"id": id, "kind": "message.created", "channel": "digest",
            "payload": {"id": id, "author": "cat-fleet", "text": text}}


def test_each_proposal_is_its_own_discord_message_in_event_order():
    events = [msg_event(1), proposal_event(142, id=2), proposal_event(143, id=3), msg_event(4), msg_event(5)]
    payloads = chat_payloads(events, "discord")
    assert len(payloads) == 4
    assert payloads[0]["content"].endswith("hi") and "embeds" not in payloads[0]
    assert [p["embeds"][0]["title"].split(":")[0] for p in payloads[1:3]] == ["P142", "P143"]
    assert "content" not in payloads[1]
    # Text after the proposals still coalesces.
    assert payloads[3]["content"].count("hi") == 2
    assert all(p["allowed_mentions"] == {"parse": []} for p in payloads)


def test_discord_embed_uses_the_fixed_template():
    embed = discord_embed(proposal_event())
    assert embed["title"] == "P142: Re-measure gate-budget units"
    assert embed["description"].startswith("Budgets rose")
    assert "Evidence:\n• testdata/gate-budget.json\n• research-2026-11-03.log" in embed["description"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields == {"Status": "open", "Project": "worldgen-v3", "Weight": "2", "Author": "cat-fleet"}
    assert all(f["inline"] for f in embed["fields"])
    assert embed["footer"]["text"] == "cat-fleet · proposal created · event 10 · react to vote"
    assert isinstance(embed["color"], int)


def test_decided_proposal_shows_who_and_why_and_no_vote_hint():
    event = proposal_event(status="rejected", previous_status="open", actor="andrew",
                           decided_by="andrew", reason="x" * 60)
    event["kind"] = "proposal_updated"
    embed = discord_embed(event)
    fields = {f["name"]: f for f in embed["fields"]}
    assert fields["Decided by"]["value"] == "andrew"
    assert fields["Reason"]["inline"] is False  # long values get their own row
    assert "react to vote" not in embed["footer"]["text"]


async def test_later_moves_keep_the_recorded_decider(hub):
    created = (await hub.create_proposal(
        author="ada", project="demo", title="Cache the wheels", summary="s", weight=3,
    ))["proposal"]
    await hub.update_proposal(created["id"], "alice", {"status": "accepted", "reason": "worth it"})
    await hub.update_proposal(created["id"], "bob", {"status": "building"})
    events = [e for e in await hub.events_after(0, channel=None, limit=100) if e["kind"] == "proposal_updated"]
    fields = {f["name"]: f["value"] for f in discord_embed(events[-1])["fields"]}
    assert fields["Status"] == "building"
    assert fields["Decided by"] == "alice" and fields["Reason"] == "worth it"
    # An open proposal has no decision to show.
    assert "Decided by" not in {f["name"] for f in discord_embed(proposal_event())["fields"]}


def test_long_evidence_is_capped():
    card = render(proposal_event(evidence=[f"item {n}" for n in range(8)]), "card")
    assert "• item 4" in card.body and "• item 5" not in card.body
    assert card.body.endswith("… and 3 more")
    assert [b.label for b in card.buttons] == ["Approve", "Reject…"]


def test_slack_still_coalesces_proposals_as_text():
    payloads = chat_payloads([proposal_event(142, id=1), proposal_event(143, id=2)], "slack")
    assert len(payloads) == 1 and "P142" in payloads[0]["text"] and "P143" in payloads[0]["text"]


async def test_proposal_events_carry_summary_and_evidence(hub):
    await hub.create_proposal(
        author="ada", project="demo", title="Cache the wheels",
        summary="s" * 2_000, weight=3, evidence=[f"e{n}" for n in range(9)],
    )
    [event] = [e for e in await hub.events_after(0, channel=None, limit=100) if e["kind"] == "proposal_created"]
    assert len(event["payload"]["summary"]) == 1_000
    assert event["payload"]["evidence"] == ["e0", "e1", "e2", "e3", "e4"]
    embed = chat_payloads([event], "discord")[0]["embeds"][0]
    assert embed["title"] == "P1: Cache the wheels" and "• e0" in embed["description"]


# ---- the reaction relay finds the proposal in the embed title ----------------


class Message:
    def __init__(self, embeds, *, webhook_id=55):
        self.id = 900
        self.clean_content = ""
        self.attachments = []
        self.embeds = embeds
        self.channel = NS(id=222, parent_id=None)
        self.author = NS(id=333, display_name="Cat Fleet", bot=True)
        self.webhook_id = webhook_id
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


@pytest.fixture
def actions(tmp_path):
    refs = relay.MessageRefs(tmp_path / "state.json")

    async def forward(payload):
        return True, ""

    return relay.ReactionActions(
        {222: relay.MapEntry("fleet", frozenset({"fleet"}))}, forward, refs, users=frozenset({111}), pace=0,
    ), refs


def embed_of(event):
    return NS(**{k: v for k, v in discord_embed(event).items() if k == "title"}, footer=None)


async def test_relay_targets_the_proposal_of_a_webhook_embed(actions):
    actions, refs = actions
    message = Message([embed_of(proposal_event(142))])
    await actions.observe(message)
    assert refs.get(900).proposal == "P142"
    assert message.reactions == ["✅", "❌"]


async def test_embed_titles_from_non_webhooks_or_naming_two_ids_target_nothing(actions):
    actions, refs = actions
    human = Message([NS(title="P142 link preview", footer=None)], webhook_id=None)
    await actions.observe(human)
    assert refs.get(900).proposal is None and human.reactions == []
    stacked = Message([NS(title="P142: a", footer=None), NS(title="P143: b", footer=None)])
    await actions.observe(stacked)
    assert refs.get(900).proposal is None and stacked.reactions == []


async def test_embed_title_naming_another_proposal_still_targets_its_own(actions):
    actions, refs = actions
    event = proposal_event(142)
    event["payload"]["title"] = "Follow up P143"
    message = Message([embed_of(event)])
    await actions.observe(message)
    assert refs.get(900).proposal == "P142"
    assert message.reactions == ["✅", "❌"]
