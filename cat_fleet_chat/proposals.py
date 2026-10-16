"""Pure helpers for proposals: validation, ids, the status machine, shapes, event payloads.

No SQL and no business rules live here. ``store.py`` reads and writes
``proposals``; ``hub.py`` decides what is allowed.

A proposal is a suggested piece of work for a project. Its public id is
``P<n>``, where ``n`` is the row id. Each proposal owns a feed root post tagged
``#proposal``; discussion is replies to that post.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from cat_fleet_chat.errors import HubError
# Projects are the same slugs knowledge facts use.
from cat_fleet_chat.knowledge import optional_project as optional_project
from cat_fleet_chat.knowledge import project_slug as project_slug

TITLE_MAX = 200
SUMMARY_MAX = 3_000
EVIDENCE_MAX = 20
EVIDENCE_ITEM_MAX = 500
REASON_MAX = 2_000
REF_MAX = 300
WEIGHT_MIN = 1
WEIGHT_MAX = 10
EVENT_SNIPPET = 120
TAG = "proposal"

STATUSES = ("open", "accepted", "rejected", "building", "built", "withdrawn")

# Allowed next statuses. ``built`` and ``withdrawn`` are terminal.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("accepted", "rejected", "withdrawn"),
    "accepted": ("building", "rejected", "withdrawn"),
    # Back to "accepted" when a build failed and the work goes back in the queue.
    "building": ("built", "accepted", "withdrawn"),
    "rejected": ("open",),
    "built": (),
    "withdrawn": (),
}

# Moving to one of these records who decided, when, and why.
DECISION_STATUSES = frozenset({"accepted", "rejected", "withdrawn"})
REASON_REQUIRED = frozenset({"rejected"})

# Fields an update may change, in display order. The command handler
# (``edit P<n> <field> <value>``) reuses this list.
EDITABLE_FIELDS = ("title", "summary", "evidence", "weight", "run_ref", "step_ref", "pr_ref")
REF_FIELDS = ("run_ref", "step_ref", "pr_ref")
# Statuses in which every editable field may change.
EDITABLE_STATUSES = frozenset({"open", "accepted"})
# Statuses in which only the refs may change (the build links its run, step and PR).
REF_EDITABLE_STATUSES = frozenset({"building", "built"})

_ID_RE = re.compile(r"^[Pp]?([1-9][0-9]{0,17})$")
# ASCII digits only: str.isdigit() also passes "²" and other digits int() rejects.
_WEIGHT_TEXT = re.compile(r"[0-9]{1,3}")


def _fail(message: str, field: str, **extra: Any) -> HubError:
    return HubError("validation_error", message, 400, {"field": field, **extra})


def public_id(number: int) -> str:
    return f"P{int(number)}"


def proposal_number(value: Any, field: str = "proposal_id") -> int:
    """The row id in ``12``, ``"12"``, ``"P12"`` or ``"p12"``."""
    if isinstance(value, bool):
        raise _fail(f"{field} must look like P12 or 12", field)
    if isinstance(value, int):
        if value < 1:
            raise _fail(f"{field} must look like P12 or 12", field)
        return value
    match = _ID_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise _fail(f"{field} must look like P12 or 12", field)
    return int(match.group(1))


def _clean_text(value: Any) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def title_text(value: Any) -> str:
    """One line, 1..200 characters."""
    if not isinstance(value, str):
        raise _fail("title must be a string", "title")
    text = " ".join(_clean_text(value).split())
    if not text:
        raise _fail("title is required", "title")
    if len(text) > TITLE_MAX:
        raise _fail(f"title exceeds {TITLE_MAX} characters", "title")
    return text


def summary_text(value: Any) -> str:
    """NFC text with ``\\n`` line endings, 1..3000 characters."""
    if not isinstance(value, str):
        raise _fail("summary must be a string", "summary")
    text = _clean_text(value)
    if not text:
        raise _fail("summary is required", "summary")
    if len(text) > SUMMARY_MAX:
        raise _fail(f"summary exceeds {SUMMARY_MAX} characters", "summary")
    return text


def evidence_list(value: Any) -> list[str]:
    """Up to 20 non-empty one-line strings (links, post refs). Missing means none."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _fail("evidence must be a list of strings", "evidence")
    if len(value) > EVIDENCE_MAX:
        raise _fail(f"evidence holds at most {EVIDENCE_MAX} items", "evidence", max=EVIDENCE_MAX)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _fail("evidence must be a list of strings", "evidence")
        text = " ".join(_clean_text(item).split())
        if not text:
            raise _fail("evidence items must not be empty", "evidence")
        if len(text) > EVIDENCE_ITEM_MAX:
            raise _fail(f"evidence items exceed {EVIDENCE_ITEM_MAX} characters", "evidence")
        items.append(text)
    return items


def weight_value(value: Any) -> int:
    """An effort estimate, an integer 1..10."""
    if isinstance(value, bool):
        raise _fail("weight must be an integer from 1 to 10", "weight")
    if isinstance(value, str) and _WEIGHT_TEXT.fullmatch(value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or not WEIGHT_MIN <= value <= WEIGHT_MAX:
        raise _fail(f"weight must be an integer from {WEIGHT_MIN} to {WEIGHT_MAX}", "weight")
    return value


def status_value(value: Any, field: str = "status") -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in STATUSES:
        raise HubError(
            "validation_error",
            f"{field} must be one of {', '.join(STATUSES)}",
            400,
            {"field": field, "allowed": list(STATUSES)},
        )
    return text


def optional_status(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return status_value(value)


def reason_text(value: Any) -> str | None:
    """Why a decision was made, up to 2000 characters. Empty means none."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail("reason must be a string", "reason")
    text = _clean_text(value)
    if len(text) > REASON_MAX:
        raise _fail(f"reason exceeds {REASON_MAX} characters", "reason")
    return text or None


def ref_text(value: Any, field: str) -> str | None:
    """A run, step or PR reference: one line up to 300 characters. Empty clears it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(f"{field} must be a string", field)
    text = _clean_text(value)
    if "\n" in text:
        raise _fail(f"{field} must be one line", field)
    if len(text) > REF_MAX:
        raise _fail(f"{field} exceeds {REF_MAX} characters", field)
    return text or None


_FIELD_CLEANERS = {
    "title": title_text,
    "summary": summary_text,
    "evidence": evidence_list,
    "weight": weight_value,
    "run_ref": lambda value: ref_text(value, "run_ref"),
    "step_ref": lambda value: ref_text(value, "step_ref"),
    "pr_ref": lambda value: ref_text(value, "pr_ref"),
}


def clean_field(name: str, value: Any) -> Any:
    """One editable field, validated. ``name`` must be in :data:`EDITABLE_FIELDS`."""
    if name not in _FIELD_CLEANERS:
        raise _fail(
            f"field must be one of {', '.join(EDITABLE_FIELDS)}", "field", allowed=list(EDITABLE_FIELDS)
        )
    return _FIELD_CLEANERS[name](value)


def clean_changes(changes: Any) -> tuple[dict[str, Any], str | None, str | None]:
    """``(fields, status, reason)`` from an update body. Unknown keys are refused."""
    if not isinstance(changes, dict):
        raise _fail("changes must be an object", "changes")
    allowed = (*EDITABLE_FIELDS, "status", "reason")
    unknown = sorted(str(key) for key in changes if key not in allowed)
    if unknown:
        raise _fail(
            f"unknown fields: {', '.join(unknown)}", unknown[0], allowed=list(allowed)
        )
    fields = {name: clean_field(name, changes[name]) for name in EDITABLE_FIELDS if name in changes}
    status = status_value(changes["status"]) if changes.get("status") not in (None, "") else None
    reason = reason_text(changes.get("reason"))
    if not fields and status is None:
        raise _fail("nothing to update: send a field or a status", "changes")
    if reason is not None and status is None:
        raise _fail("reason goes with a status change", "reason")
    return fields, status, reason


def check_transition(current: str, target: str) -> None:
    """Refuse a status change the table does not allow, naming the allowed next statuses."""
    allowed = TRANSITIONS.get(current, ())
    if target in allowed:
        return
    if not allowed:
        message = f"proposal is {current}, a final status; it cannot change"
    elif target == current:
        message = f"proposal is already {current}; next can be {', '.join(allowed)}"
    else:
        message = f"cannot move a proposal from {current} to {target}; next can be {', '.join(allowed)}"
    raise HubError(
        "invalid_transition",
        message,
        409,
        {"field": "status", "from": current, "to": target, "allowed": list(allowed)},
    )


def check_editable(status: str, fields: dict[str, Any]) -> None:
    """Refuse field edits the proposal's status does not allow."""
    if not fields or status in EDITABLE_STATUSES:
        return
    if status in REF_EDITABLE_STATUSES:
        locked = sorted(name for name in fields if name not in REF_FIELDS)
        if not locked:
            return
        message = f"a {status} proposal only takes {', '.join(REF_FIELDS)}; not {', '.join(locked)}"
        allowed = list(REF_FIELDS)
    else:
        message = f"a {status} proposal cannot be edited"
        allowed = []
    raise HubError("proposal_locked", message, 409, {"status": status, "allowed": allowed})


def post_body(number: int, title: str, summary: str) -> str:
    """The feed root post: ``P<n>: title``, the summary, and the ``#proposal`` tag."""
    return f"{public_id(number)}: {title}\n\n{summary}\n\n#{TAG}"


def evidence_json(items: list[str]) -> str:
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _evidence(raw: Any) -> list[str]:
    try:
        parsed = json.loads(raw) if raw else []
    except (ValueError, RecursionError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def shape_proposal(row: dict[str, Any]) -> dict[str, Any]:
    """API shape of one proposal row. Every surface shapes through here."""
    number = int(row["id"])
    return {
        "id": public_id(number),
        "number": number,
        "project": row["project"],
        "title": row["title"],
        "summary": row["summary"],
        "evidence": _evidence(row["evidence"]),
        "weight": int(row["weight"]),
        "status": row["status"],
        "author": row["author"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "reason": row["reason"],
        "run_ref": row["run_ref"],
        "step_ref": row["step_ref"],
        "pr_ref": row["pr_ref"],
        "post_id": row["post_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def event_payload(
    proposal: dict[str, Any],
    *,
    actor: str,
    previous_status: str | None = None,
    changed: list[str] | None = None,
) -> dict[str, Any]:
    """A small snippet for ``proposal_created`` / ``proposal_updated`` events."""
    payload: dict[str, Any] = {
        "id": proposal["id"],
        "number": proposal["number"],
        "project": proposal["project"],
        "title": proposal["title"][:EVENT_SNIPPET],
        "status": proposal["status"],
        "weight": proposal["weight"],
        "author": proposal["author"],
        "actor": actor,
        "post_id": proposal["post_id"],
    }
    if previous_status is not None:
        payload["previous_status"] = previous_status
        if proposal["reason"]:
            payload["reason"] = proposal["reason"][:EVENT_SNIPPET]
    if changed is not None:
        payload["changed"] = changed
    return payload
