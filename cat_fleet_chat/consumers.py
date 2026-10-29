"""Durable consumers: pure helpers for filters, matching and audit shaping.

A consumer is a named reader of the event log with its own cursor, the
per-subscription cursor topic pub/sub does not have. Validation, matching
and redaction only; SQL lives in ``store.py`` and the rules (lease, ack,
seek, events, audit) in ``relay_hub.py``.

Delivery is at-least-once: ``wait`` returns events after the cursor and never
moves it past them; only ``ack`` does. Consumers dedupe by event ``id``.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from typing import Any

from cat_fleet_chat import pubsub
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.validate import (
    CHANNEL_RE,
    EVENT_KINDS,
    HANDLE_RE,
)

# Each filter list holds at most this many entries.
FILTER_MAX = 50
# Consumers in one hub, so a wake never scans an unbounded list.
CONSUMERS_MAX = 500
DESCRIPTION_MAX = 500
# A wait parks at most this long and returns at most this many events.
WAIT_MAX = 45
WAIT_DEFAULT = 30
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
# A wait keeps the consumer "armed" for its timeout plus this grace.
LEASE_GRACE = 15
# Audit rows kept; older rows are dropped on insert.
AUDIT_KEEP = 10_000
# Serialized before/after JSON in one audit row.
AUDIT_VALUE_MAX = 8_000
# Keys never written to the audit log, whatever object they sit in.
SECRET_KEYS = frozenset({"secret", "token", "token_hash", "api_key", "password"})
DESTINATION_MAX = 80

FILTER_FIELDS = ("kinds", "topics", "channels", "task_ids", "destinations", "exclude_authors")


def _fail(message: str, field: str) -> HubError:
    return HubError("validation_error", message, 400, {"field": field})


def _items(value: Any, field: str) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        raise _fail(f"{field} must be a list", field)
    if len(value) > FILTER_MAX:
        raise _fail(f"{field} holds at most {FILTER_MAX} entries", field)
    return list(value)


def _unique(items: list[Any]) -> list[Any]:
    seen: list[Any] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def kinds(value: Any) -> list[str]:
    items = _items(value, "kinds")
    unknown = sorted(str(item) for item in items if item not in EVENT_KINDS)
    if unknown:
        raise HubError(
            "validation_error",
            "unknown event kind: " + ", ".join(unknown),
            400,
            {"field": "kinds", "allowed": sorted(EVENT_KINDS)},
        )
    return _unique(items)


def topics(value: Any) -> list[str]:
    return _unique([pubsub.pattern(item, "topics") for item in _items(value, "topics")])


def channels(value: Any) -> list[str]:
    items = _items(value, "channels")
    if not all(isinstance(item, str) and CHANNEL_RE.fullmatch(item) for item in items):
        raise _fail("channels must be channel names", "channels")
    return _unique(items)


def task_ids(value: Any) -> list[int]:
    out: list[int] = []
    for item in _items(value, "task_ids"):
        if isinstance(item, bool):
            raise _fail("task_ids must be positive integers", "task_ids")
        try:
            number = int(item)
        except (TypeError, ValueError) as exc:
            raise _fail("task_ids must be positive integers", "task_ids") from exc
        if number < 1:
            raise _fail("task_ids must be positive integers", "task_ids")
        out.append(number)
    return _unique(out)


def destinations(value: Any) -> list[str]:
    items = _items(value, "destinations")
    if not all(isinstance(item, str) and 0 < len(item) <= DESTINATION_MAX for item in items):
        raise _fail(f"destinations must be 1..{DESTINATION_MAX} character names", "destinations")
    return _unique(items)


def exclude_authors(value: Any) -> list[str]:
    items = _items(value, "exclude_authors")
    if not all(isinstance(item, str) and 0 < len(item) <= 64 for item in items):
        raise _fail("exclude_authors must be handle globs", "exclude_authors")
    return _unique(items)


CLEANERS = {
    "kinds": kinds,
    "topics": topics,
    "channels": channels,
    "task_ids": task_ids,
    "destinations": destinations,
    "exclude_authors": exclude_authors,
}


def name(value: Any, field: str = "name") -> str:
    """A consumer name: the handle grammar, so it is a safe path segment."""
    if not isinstance(value, str) or not HANDLE_RE.fullmatch(value):
        raise _fail(f"{field} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}", field)
    return value


def description(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > DESCRIPTION_MAX:
        raise _fail(f"description must be at most {DESCRIPTION_MAX} characters", "description")
    return value.strip() or None


def filters(body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Clean filter fields from ``body``. ``partial`` keeps only the fields present."""
    out: dict[str, Any] = {}
    for field in FILTER_FIELDS:
        if partial and field not in body:
            continue
        out[field] = CLEANERS[field](body.get(field))
    return out


def wait_seconds(value: Any) -> int:
    """``0`` returns at once; otherwise 1..:data:`WAIT_MAX`, default :data:`WAIT_DEFAULT`."""
    if value is None or value == "":
        return WAIT_DEFAULT
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail("timeout must be an integer", "timeout") from exc
    return max(0, min(seconds, WAIT_MAX))


def wait_limit(value: Any) -> int:
    if value is None or value == "":
        return LIMIT_DEFAULT
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail("limit must be an integer", "limit") from exc
    if number < 1:
        raise _fail("limit must be at least 1", "limit")
    return min(number, LIMIT_MAX)


# ---- matching -------------------------------------------------------------


def event_actor(event: dict[str, Any]) -> str | None:
    """Who caused ``event``: message author, last task actor, or channel actor."""
    payload = event.get("payload") or {}
    kind = event["kind"]
    if kind == "message.created":
        return payload.get("author")
    if kind == "task.updated":
        history = payload.get("events") or []
        return history[-1].get("actor") if history else None
    if kind in {"feed_post", "feed_repost", "knowledge_fact"}:
        return payload.get("author")
    if kind in {"proposal_created", "proposal_updated"}:
        return payload.get("actor") or payload.get("author")
    if kind == "feed_follow":
        return payload.get("follower")
    if kind in {"reaction_added", "reaction_removed"}:
        return payload.get("handle")
    if kind == "mail":
        return payload.get("sender")
    if kind == "topic.published":
        return payload.get("publisher")
    if kind in {"topic.subscribed", "topic.unsubscribed"}:
        return payload.get("subscriber")
    return payload.get("actor")


def matches(consumer: dict[str, Any], event: dict[str, Any]) -> bool:
    """Whether ``event`` is delivered to ``consumer``. Every non-empty filter must pass.

    ``topics`` patterns constrain ``topic.published`` events; with no
    ``kinds`` they also limit the consumer to those events. ``task_ids``
    constrains ``task.updated`` events. ``destinations`` keeps only messages
    addressed to one of them. ``mail`` bodies never ride events, so a mail
    notice is delivered like any other kind.
    """
    kind = event["kind"]
    payload = event.get("payload") or {}
    wanted = consumer.get("kinds") or []
    patterns = consumer.get("topics") or []
    if wanted:
        if kind not in wanted:
            return False
    elif patterns and kind != "topic.published":
        return False
    if patterns and kind == "topic.published":
        if not pubsub.matches_any(patterns, str(payload.get("topic", ""))):
            return False
    if consumer.get("channels") and event.get("channel") not in consumer["channels"]:
        return False
    if consumer.get("task_ids") and kind == "task.updated":
        if payload.get("id") not in consumer["task_ids"]:
            return False
    if consumer.get("destinations"):
        if kind != "message.created" or payload.get("destination") not in consumer["destinations"]:
            return False
    actor = event_actor(event)
    if actor and any(fnmatchcase(actor, glob) for glob in consumer.get("exclude_authors") or []):
        return False
    return True


# ---- audit ----------------------------------------------------------------


def scrub(value: Any) -> Any:
    """Drop secret-looking keys anywhere in ``value``."""
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items() if key not in SECRET_KEYS}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def audit_value(value: Any) -> str | None:
    """Scrubbed JSON for an audit column, cut to :data:`AUDIT_VALUE_MAX` characters."""
    if value is None:
        return None
    text = json.dumps(scrub(value), separators=(",", ":"), default=str)
    if len(text) > AUDIT_VALUE_MAX:
        return json.dumps({"truncated": True, "head": text[: AUDIT_VALUE_MAX - 64]})
    return text


def event_payload(consumer: dict[str, Any], actor: str) -> dict[str, Any]:
    """The ``consumer.*`` event body: identity, owner, actor and cursor; never filters' secrets."""
    return {
        "name": consumer["name"],
        "owner": consumer["owner"],
        "actor": actor,
        "cursor": consumer["cursor"],
        "relay_id": consumer.get("relay_id"),
    }
