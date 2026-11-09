"""The fleet's reserved question author is bound to a credential (Sol review of a5ded2e).

Only a CAT_FLEET_RESERVED_AUTHOR_TOKENS secret may post as a reserved author, and
only such a post (without origin) carries ``fleet_author`` provenance into its
event, which is what the Discord relays trust for question reactions.
"""
from __future__ import annotations

import pytest

from cat_fleet_chat import mcp_app
from cat_fleet_chat.config import Settings
from cat_fleet_chat.webhooks import chat_payloads

from conftest import _open

FLEET = {"Authorization": "Bearer fleet-secret"}
TOKEN = {"Authorization": "Bearer test-token"}


@pytest.fixture
async def reserved(tmp_path):
    settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token="test-token",
        dev_origins=("http://127.0.0.1:3001",),
        webhooks_enabled=False,
        reserved_author_tokens=frozenset({"fleet-secret"}),
    )
    async for item in _open(settings):
        yield item


async def _message_events(hub) -> list[dict]:
    events, _, _ = await hub.scan_events(0, agent=None, kinds=frozenset({"message.created"}), channel=None, limit=100)
    return events


def _footer_author(event) -> str | None:
    import json

    text = chat_payloads([event], "discord", refs=True)[0]["embeds"][0]["footer"]["text"]
    return json.loads(text[len("cat-fleet-ref:"):]).get("author")


@pytest.mark.parametrize("author", ["cat-fleet", "Cat-Fleet", " cat-fleet "])
async def test_fleet_token_rest_client_cannot_post_as_reserved_author(reserved, author):
    client, _app = reserved
    response = await client.post("/api/v1/messages", json={"channel": "fleet", "author": author, "text": "[S03] x\n(a) yes"},
                                 headers=TOKEN)
    # 403 for the reserved author; a padded handle is already invalid (400).
    assert response.status_code == (400 if author != author.strip() else 403)


async def test_mcp_client_cannot_post_as_reserved_author(reserved):
    _client, app = reserved
    mcp_app.set_hub(app.state.hub)
    try:
        refused = await mcp_app.post_message("fleet", "cat-fleet", "[LEAD run Q1] x: Which?\n(a) yes")
        assert refused["error"] == "forbidden" and refused["http_status"] == 403
        assert "message" in await mcp_app.post_message("fleet", "codex", "fine")
    finally:
        mcp_app.set_hub(None)


async def test_reserved_author_token_posts_with_fleet_provenance(reserved):
    client, app = reserved
    response = await client.post("/api/v1/messages", json={"channel": "fleet", "author": "cat-fleet", "text": "q"},
                                 headers=FLEET)
    assert response.status_code == 201
    # The secret is a full fleet credential (the fleet uses it for every call).
    assert (await client.get("/api/v1/channels", headers=FLEET)).status_code == 200
    # Another author through the same secret is ordinary, and so is a relayed post.
    await client.post("/api/v1/messages", json={"channel": "fleet", "author": "codex", "text": "q2"}, headers=FLEET)
    origin = {"source": "discord", "channel_id": "222", "message_id": "1"}
    relayed = await client.post("/api/v1/messages",
                                json={"channel": "fleet", "author": "cat-fleet", "text": "q3", "origin": origin},
                                headers=FLEET)
    assert relayed.status_code == 201
    events = await _message_events(app.state.hub)
    assert [e["payload"].get("fleet_author") for e in events] == [True, None, None]
    assert [_footer_author(e) for e in events] == ["cat-fleet", None, None]
    # The stored message itself never shows the flag.
    message = response.json()["message"]
    assert "fleet_author" not in message


async def test_wrong_secret_is_unauthorized(reserved):
    client, _app = reserved
    bad = {"Authorization": "Bearer fleet-secret-x"}
    assert (await client.get("/api/v1/channels", headers=bad)).status_code == 401


async def test_without_reserved_tokens_nobody_gets_provenance(authed):
    client, app = authed
    response = await client.post("/api/v1/messages", json={"channel": "fleet", "author": "cat-fleet", "text": "q"},
                                 headers=TOKEN)
    assert response.status_code == 201  # the fleet keeps posting until the secret is configured
    events = await _message_events(app.state.hub)
    assert events[-1]["payload"].get("fleet_author") is None and _footer_author(events[-1]) is None


def test_reserved_author_tokens_setting(monkeypatch):
    from cat_fleet_chat.config import load_settings

    monkeypatch.delenv("CAT_FLEET_RESERVED_AUTHOR_TOKENS", raising=False)
    assert load_settings().reserved_author_tokens == frozenset()
    monkeypatch.setenv("CAT_FLEET_RESERVED_AUTHOR_TOKENS", " a1 , ,b2")
    settings = load_settings()
    assert settings.reserved_author_tokens == frozenset({"a1", "b2"})
    assert "a1" not in repr(settings)
