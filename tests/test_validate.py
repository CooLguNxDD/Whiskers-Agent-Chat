"""Mention parsing and input rules."""

from cat_fleet_chat.errors import HubError
from cat_fleet_chat.validate import parse_mentions, transition_allowed


def test_mentions_skip_email_and_dedupe():
    text = "ping ada@example.com and @codex, then @codex again, plus @Claude-Code"
    assert parse_mentions(text) == ["codex", "Claude-Code"]


def test_mentions_require_a_boundary():
    assert parse_mentions("foo@codex") == []
    assert parse_mentions("(@codex)") == ["codex"]
    assert parse_mentions("see @a") == ["a"]


def test_terminal_states_do_not_move():
    assert transition_allowed("done", "open") is False
    assert transition_allowed("open", "claimed") is True
    assert transition_allowed("blocked", "open") is True
    assert transition_allowed("in_progress", "claimed") is False


def test_bind_without_token_refuses_public_host():
    from cat_fleet_chat.config import Settings, assert_bind_safe

    settings = Settings(
        host="0.0.0.0",
        port=8787,
        db_path="x.sqlite",
        token=None,
        dev_origins=(),
    )
    try:
        assert_bind_safe(settings)
    except SystemExit as exc:
        assert "CAT_FLEET_TOKEN" in str(exc)
    else:
        raise AssertionError("expected SystemExit")

    assert_bind_safe(
        Settings(host="127.0.0.1", port=8787, db_path="x", token=None, dev_origins=())
    )


def test_hub_error_envelopes():
    err = HubError("channel_exists", "taken", 409, {"name": "fleet"})
    assert err.rest_body()["error"]["code"] == "channel_exists"
    assert err.mcp_body()["status"] == "error"
    assert err.mcp_body()["error"] == "channel_exists"
