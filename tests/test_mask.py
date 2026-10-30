"""Credential masking for relayed text."""

from __future__ import annotations

import pytest

from cat_fleet_chat.mask import mask

# Synthetic shapes only; none of these are real credentials.
CASES = [
    ("relay-token", "rly_" + "a" * 30),
    ("discord-token", "M" + "a" * 23 + "." + "b" * 6 + "." + "c" * 27),
    ("slack-token", "xoxb-" + "1" * 12 + "-abcdef"),
    ("slack-app-token", "xapp-1-" + "A" * 20),
    ("slack-webhook", "https://hooks.slack.com/services/T000/B000/" + "x" * 24),
    ("discord-webhook", "https://discord.com/api/webhooks/123456/" + "y" * 40),
    ("telegram-token", "123456789:" + "A" * 35),
    ("github-token", "ghp_" + "a" * 36),
    ("anthropic-key", "sk-ant-" + "a" * 30),
    ("openai-key", "sk-proj-" + "a" * 30),
    ("google-api-key", "AIza" + "a" * 35),
    ("aws-access-key", "AKIA" + "A" * 16),
    ("jwt", "eyJ" + "a" * 10 + ".eyJ" + "b" * 10 + "." + "c" * 10),
    ("bearer", "Bearer " + "z" * 30),
    ("password-assignment", "password=hunter2hunter2"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----"),
]


@pytest.mark.parametrize(("kind", "secret"), CASES)
def test_masks_each_shape(kind, secret):
    text, found = mask(f"look: {secret} ok")
    assert secret not in text
    assert f"[secret masked: {kind}]" in text
    assert found == [kind]


def test_plain_text_untouched():
    text = "deploy ci.web.done in #ops, see https://example.com/a/b and P12"
    assert mask(text) == (text, [])


def test_multiple_kinds_reported_once_in_order():
    text, found = mask("ghp_" + "a" * 36 + " and ghp_" + "b" * 36 + " and AKIA" + "B" * 16)
    assert found == ["github-token", "aws-access-key"]
    assert text.count("[secret masked: github-token]") == 2
