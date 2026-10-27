"""Topic pub/sub: pure helpers for topics, subscription patterns and payloads.

Validation and matching only. SQL lives in ``store.py`` and the rules
(idempotency, events, who is notified) in ``hub.py``.

A topic is dot-separated segments, e.g. ``build.whiskers.done``. A
subscription pattern is a topic whose segments may also be ``*`` (exactly one
segment) or, as the last segment only, ``>`` (one or more segments), so
``build.*.done`` and ``build.>`` both match ``build.whiskers.done``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from cat_fleet_chat.errors import HubError

SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEGMENTS_MAX = 8
TOPIC_MAX = 200
# Serialized JSON size of ``data``. Same cap as a chat message.
DATA_MAX = 16_000
# Subscriptions one handle may hold, so a wake never scans an unbounded list.
SUBSCRIPTIONS_MAX = 100
ONE = "*"
REST = ">"


def _fail(message: str, field: str) -> HubError:
    """A 400 ``validation_error`` naming ``field``."""
    return HubError("validation_error", message, 400, {"field": field})


def _segments(value: Any, field: str) -> list[str]:
    """Split a topic or pattern into segments, checking total length and segment count."""
    if not isinstance(value, str) or not value or len(value) > TOPIC_MAX:
        raise _fail(f"{field} must be 1..{TOPIC_MAX} characters", field)
    parts = value.split(".")
    if len(parts) > SEGMENTS_MAX:
        raise _fail(f"{field} has more than {SEGMENTS_MAX} segments", field)
    return parts


def topic(value: Any, field: str = "topic") -> str:
    """A concrete topic: segments of ``[a-z0-9][a-z0-9_-]{0,63}`` joined by dots."""
    for part in _segments(value, field):
        if not SEGMENT_RE.fullmatch(part):
            raise _fail(
                f"{field} segments must match [a-z0-9][a-z0-9_-]{{0,63}} (no wildcards)", field
            )
    return value


def pattern(value: Any, field: str = "pattern") -> str:
    """A subscription pattern: a topic whose segments may be ``*``, or ``>`` last."""
    parts = _segments(value, field)
    for index, part in enumerate(parts):
        if part == ONE or SEGMENT_RE.fullmatch(part):
            continue
        if part == REST and index == len(parts) - 1:
            continue
        raise _fail(
            f"{field} segments must be a topic segment, '*', or a trailing '>'", field
        )
    return value


def matches(subscription: str, name: str) -> bool:
    """Whether ``subscription`` (a valid pattern) matches the topic ``name``."""
    want = subscription.split(".")
    have = name.split(".")
    for index, part in enumerate(want):
        if part == REST:
            return len(have) > index
        if index >= len(have):
            return False
        if part != ONE and part != have[index]:
            return False
    return len(want) == len(have)


def matches_any(patterns: Any, name: str) -> bool:
    """Whether any of ``patterns`` matches the topic ``name``."""
    return any(matches(item, name) for item in patterns)


def literal_prefix(subscription: str) -> str:
    """The segments before the first wildcard, for a SQL prefix filter. ``''`` if none."""
    out: list[str] = []
    for part in subscription.split("."):
        if part in (ONE, REST):
            break
        out.append(part)
    return ".".join(out)


def data(value: Any) -> Any:
    """Any JSON value up to :data:`DATA_MAX` serialized characters. ``None`` is allowed."""
    try:
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _fail("data must be JSON-serializable", "data") from exc
    if len(encoded) > DATA_MAX:
        raise _fail(f"data exceeds {DATA_MAX} characters as JSON", "data")
    return value


def event_payload(publication: dict[str, Any]) -> dict[str, Any]:
    """The ``topic.published`` event body. It carries ``data`` so SSE and hooks need no lookup."""
    return {
        "id": publication["id"],
        "topic": publication["topic"],
        "publisher": publication["publisher"],
        "data": publication["data"],
    }
