"""Agent mailbox: pure helpers for direct mail between handles.

Validation and row shaping only. SQL lives in ``store.py`` and the rules
(who may read or ack, idempotency, events, wakes) in ``hub.py``.
"""

from __future__ import annotations

from typing import Any

from cat_fleet_chat.errors import HubError
from cat_fleet_chat.validate import handle, optional_id, require_text

SUBJECT_MAX = 200
# Same cap as a chat message.
BODY_MAX = 16_000
# How many ids one read or ack call may name.
IDS_MAX = 500
MAIL_STATES = ("unread", "read", "all")


def _fail(message: str, field: str) -> HubError:
    return HubError("validation_error", message, 400, {"field": field})


def recipient(value: Any) -> str:
    """Any valid handle. Agents are free-form, so an unknown handle is fine."""
    return handle(value, "recipient")


def sender(value: Any) -> str:
    return handle(value, "sender")


def subject(value: Any) -> str:
    """Optional one-line subject: ``''`` when absent, line breaks folded to spaces."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _fail("subject must be a string", "subject")
    folded = " ".join(value.split())
    if len(folded) > SUBJECT_MAX:
        raise _fail(f"subject exceeds {SUBJECT_MAX} characters", "subject")
    return folded


def body(value: Any) -> str:
    return require_text(value, "body", BODY_MAX)


def reply_to(value: Any) -> int | None:
    if isinstance(value, bool):
        raise _fail("reply_to must be a mail id", "reply_to")
    try:
        parsed = optional_id(value, "reply_to")
    except HubError as exc:
        raise _fail(exc.message, "reply_to") from exc
    return parsed or None


def state(value: Any) -> str:
    if value in (None, ""):
        return "unread"
    if not isinstance(value, str) or value not in MAIL_STATES:
        raise HubError(
            "validation_error",
            f"state must be one of {', '.join(MAIL_STATES)}",
            400,
            {"field": "state", "allowed": list(MAIL_STATES)},
        )
    return value


def ids(value: Any, field: str = "ids") -> list[int]:
    """A non-empty list of mail ids (a comma string is accepted), deduplicated in order."""
    if isinstance(value, str):
        items: Any = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, int) and not isinstance(value, bool):
        items = [value]
    else:
        items = value
    if not isinstance(items, (list, tuple)) or not items:
        raise _fail(f"{field} must be a non-empty list of mail ids", field)
    if len(items) > IDS_MAX:
        raise _fail(f"{field} may name at most {IDS_MAX} mail ids", field)
    seen: list[int] = []
    for item in items:
        if isinstance(item, bool):
            raise _fail(f"{field} must hold integers", field)
        parsed = optional_id(item, field)
        if parsed is None or parsed < 1:
            raise _fail(f"{field} must hold positive integers", field)
        if parsed not in seen:
            seen.append(parsed)
    return seen


def mail_state(row: dict[str, Any]) -> str:
    if row.get("acked_at"):
        return "acked"
    if row.get("read_at"):
        return "read"
    return "unread"


def mail_row(row: Any) -> dict[str, Any]:
    """One mail as the API shows it to its recipient or sender."""
    found = dict(row)
    shaped = {
        "id": found["id"],
        "recipient": found["recipient"],
        "sender": found["sender"],
        "subject": found["subject"],
        "body": found["body"],
        "created_at": found["created_at"],
        "read_at": found["read_at"],
        "acked_at": found["acked_at"],
        "reply_to": found["reply_to"],
        "message_id": found["message_id"],
    }
    shaped["state"] = mail_state(shaped)
    return shaped


def event_payload(mail: dict[str, Any]) -> dict[str, Any]:
    """What the ``mail`` event carries. Never the body: mail is private."""
    return {
        "id": mail["id"],
        "recipient": mail["recipient"],
        "sender": mail["sender"],
        "subject": mail["subject"],
        "reply_to": mail["reply_to"],
        "created_at": mail["created_at"],
    }
