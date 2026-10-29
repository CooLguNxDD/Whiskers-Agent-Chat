"""Credential masking for text that crosses a chat bridge.

People paste tokens into Discord, Slack and Telegram by accident. A relay
masks credential shapes before it forwards text to the hub (and before it
posts hub text out), so a pasted secret does not end up in the durable
event log, the search index or another platform. Pure: no I/O.

Masking is best effort on well-known shapes. It is not a substitute for
rotating a credential that was pasted.
"""

from __future__ import annotations

import re

# (kind, pattern). Ordered: specific shapes first, so a Slack token is not
# reported as a generic bearer token.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
    ),
    ("relay-token", re.compile(r"\brly_[A-Za-z0-9_-]{20,}")),
    ("discord-token", re.compile(r"\b[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}\b")),
    ("slack-token", re.compile(r"\bxox[abeoprs]-[A-Za-z0-9-]{10,}")),
    ("slack-app-token", re.compile(r"\bxapp-[0-9]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}")),
    ("discord-webhook", re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]{20,}")),
    ("telegram-token", re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*")),
    (
        "password-assignment",
        re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[\"']?[^\s\"']{8,}"),
    ),
)


def placeholder(kind: str) -> str:
    return f"[secret masked: {kind}]"


def mask(text: str) -> tuple[str, list[str]]:
    """``text`` with credential shapes replaced, plus the kinds found (in order, unique)."""
    found: list[str] = []
    for kind, pattern in PATTERNS:

        def replace(_match: re.Match[str], kind: str = kind) -> str:
            if kind not in found:
                found.append(kind)
            return placeholder(kind)

        text = pattern.sub(replace, text)
    return text, found
