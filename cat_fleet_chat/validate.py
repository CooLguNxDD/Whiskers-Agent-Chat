"""Input rules shared by every adapter. Parsing mentions happens once, at write time."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from cat_fleet_chat.errors import HubError

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# A mention is an @handle at a token boundary. Characters that appear in an
# email local-part or immediately before @ suppress the match, so
# "ada@example.com" is not a mention of "example".
MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9@._%+-])@([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![A-Za-z0-9_-])"
)

TEXT_MAX = 16_000
TITLE_MAX = 200
TOPIC_MAX = 500
NOTE_MAX = 2_000
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
TIMEOUT_DEFAULT = 60
TIMEOUT_MIN = 1
TIMEOUT_MAX = 300

TASK_STATUSES = frozenset(
    {"open", "claimed", "in_progress", "blocked", "done", "cancelled"}
)
TERMINAL_STATUSES = frozenset({"done", "cancelled"})

# Channel lifecycle, in display order. Only "archived" changes behavior: it is
# the read-only archive and follows the archive rules (open tasks, force).
CHANNEL_STATES = ("active", "paused", "blocked", "review", "done", "archived")

EVENT_KINDS = frozenset(
    {
        "message.created",
        "task.updated",
        "channel.created",
        "channel.archived",
        "channel.unarchived",
        "channel.state_changed",
    }
)

ATTACHMENTS_MAX = 10
ATTACHMENT_SIZE_MAX = 100 * 1024 * 1024
ATTACHMENT_STORAGES = frozenset({"minio"})
FILENAME_MAX = 255
OBJECT_KEY_MAX = 1024
# S3/MinIO bucket naming: 3..63 lowercase letters, digits, dots, hyphens.
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Terminal states are absent: v1 does not reopen them.
TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"claimed", "cancelled"}),
    "claimed": frozenset({"in_progress", "blocked", "open", "cancelled"}),
    "in_progress": frozenset({"blocked", "done", "cancelled"}),
    "blocked": frozenset({"in_progress", "open", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}


def _fail(message: str, *, details: dict[str, Any] | None = None) -> HubError:
    return HubError("validation_error", message, 400, details)


def require_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise _fail(f"{field} is required")
    if len(text) > limit:
        raise _fail(f"{field} exceeds {limit} characters")
    return text


def optional_text(value: Any, field: str, limit: int, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise _fail(f"{field} must be a string")
    if len(value) > limit:
        raise _fail(f"{field} exceeds {limit} characters")
    return value.strip()


def channel_name(value: Any) -> str:
    if not isinstance(value, str) or not CHANNEL_RE.fullmatch(value):
        raise _fail(
            "channel name must match [a-z0-9][a-z0-9_-]{0,63}",
            details={"field": "channel"},
        )
    return value


def handle(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HANDLE_RE.fullmatch(value):
        raise _fail(
            f"{field} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}",
            details={"field": field},
        )
    return value


def optional_handle(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return handle(value, field)


def parse_mentions(text: str) -> list[str]:
    """Deduplicated @handles in first-seen order. Email addresses are skipped."""
    seen: set[str] = set()
    found: list[str] = []
    for match in MENTION_RE.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        found.append(name)
    return found


def page_limit(value: Any) -> int:
    if value is None or value == "":
        return LIMIT_DEFAULT
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail("limit must be an integer") from exc
    if parsed < 1:
        raise _fail("limit must be at least 1")
    return min(parsed, LIMIT_MAX)


def optional_id(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail(f"{field} must be an integer") from exc
    if parsed < 0:
        raise _fail(f"{field} must be >= 0")
    return parsed


def require_id(value: Any, field: str) -> int:
    parsed = optional_id(value, field)
    if parsed is None or parsed < 1:
        raise _fail(f"{field} is required")
    return parsed


def clamp_timeout(value: Any) -> int:
    if value is None or value == "":
        return TIMEOUT_DEFAULT
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail("timeout must be an integer") from exc
    if parsed < TIMEOUT_MIN:
        return TIMEOUT_MIN
    if parsed > TIMEOUT_MAX:
        return TIMEOUT_MAX
    return parsed


def channel_state(value: Any, field: str = "state") -> str:
    if not isinstance(value, str) or value not in CHANNEL_STATES:
        raise _fail(
            f"{field} must be one of {', '.join(CHANNEL_STATES)}",
            details={"field": field, "allowed": list(CHANNEL_STATES)},
        )
    return value


def channel_states(value: Any) -> frozenset[str] | None:
    """Comma string or list of channel states for filtering. Empty means no filter."""
    if value is None or value == "" or value == []:
        return None
    items = [part.strip() for part in value.split(",")] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise _fail("state must be a comma-separated string or a list", details={"field": "state"})
    return frozenset(channel_state(item) for item in items if item) or None


def wait_timeout(value: Any) -> int:
    """Like :func:`clamp_timeout`, but an explicit ``0`` means return immediately."""
    if value in (0, "0"):
        return 0
    return clamp_timeout(value)


def event_kinds(value: Any) -> frozenset[str] | None:
    """Comma string or list of event kinds. ``None`` or empty means every kind."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise _fail("kinds must be a comma-separated string or a list", details={"field": "kinds"})
    unknown = sorted(str(item) for item in items if item not in EVENT_KINDS)
    if unknown:
        raise _fail(
            "unknown event kind: " + ", ".join(unknown),
            details={"field": "kinds", "allowed": sorted(EVENT_KINDS)},
        )
    return frozenset(items) or None


WEBHOOK_DIRECTIONS = ("out", "in")
WEBHOOK_FORMATS = ("discord", "slack", "generic")
WEBHOOK_LIST_MAX = 50
WEBHOOK_PATTERN_MAX = 64
WEBHOOK_URL_MAX = 2_000


def webhook_name(value: Any) -> str:
    if not isinstance(value, str) or not CHANNEL_RE.fullmatch(value):
        raise _fail(
            "webhook name must match [a-z0-9][a-z0-9_-]{0,63}",
            details={"field": "name"},
        )
    return value


def webhook_direction(value: Any) -> str:
    if not isinstance(value, str) or value not in WEBHOOK_DIRECTIONS:
        raise _fail(
            f"direction must be one of {', '.join(WEBHOOK_DIRECTIONS)}",
            details={"field": "direction", "allowed": list(WEBHOOK_DIRECTIONS)},
        )
    return value


def webhook_format(value: Any) -> str:
    if value is None or value == "":
        return "generic"
    if not isinstance(value, str) or value not in WEBHOOK_FORMATS:
        raise _fail(
            f"format must be one of {', '.join(WEBHOOK_FORMATS)}",
            details={"field": "format", "allowed": list(WEBHOOK_FORMATS)},
        )
    return value


def webhook_url(value: Any) -> str:
    """http(s) URL. Plain http is only accepted for a loopback host."""
    from urllib.parse import urlsplit

    from cat_fleet_chat.config import is_loopback_host

    text = require_text(value, "url", WEBHOOK_URL_MAX)
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError as exc:
        raise _fail("url is not valid", details={"field": "url"}) from exc
    if parts.scheme not in {"http", "https"} or not host:
        raise _fail("url must be an http(s) URL", details={"field": "url"})
    if parts.scheme == "http" and not is_loopback_host(host):
        raise _fail("url must use https unless the host is loopback", details={"field": "url"})
    return text


def _string_list(value: Any, field: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items: Any = [part.strip() for part in value.split(",") if part.strip()]
    else:
        items = value
    if not isinstance(items, (list, tuple)):
        raise _fail(f"{field} must be a list or comma-separated string", details={"field": field})
    if len(items) > WEBHOOK_LIST_MAX:
        raise _fail(f"{field} has more than {WEBHOOK_LIST_MAX} entries", details={"field": field})
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise _fail(f"{field} entries must be strings", details={"field": field})
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def webhook_kinds(value: Any) -> list[str]:
    return sorted(event_kinds(value) or ())


def webhook_channels(value: Any) -> list[str]:
    return [channel_name(item) for item in _string_list(value, "channels")]


def webhook_handles(value: Any, field: str) -> list[str]:
    return [handle(item, field) for item in _string_list(value, field)]


def webhook_patterns(value: Any, field: str) -> list[str]:
    items = _string_list(value, field)
    for item in items:
        if len(item) > WEBHOOK_PATTERN_MAX:
            raise _fail(
                f"{field} entries must be at most {WEBHOOK_PATTERN_MAX} characters",
                details={"field": field},
            )
    return items


ORIGIN_SOURCES = ("discord",)
DESCRIPTION_MAX = 200
ORIGIN_AUTHOR_MAX = 80
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,25}$")


def snowflake(value: Any, field: str) -> str:
    """A Discord id as a digit string. Ints are accepted, but ids stay strings: they overflow JS numbers."""
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not SNOWFLAKE_RE.fullmatch(text):
        raise _fail(f"{field} must be a Discord id (digits only)", details={"field": field})
    return text


def optional_snowflake(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return snowflake(value, field)


def webhook_description(value: Any) -> str | None:
    text = optional_text(value, "description", DESCRIPTION_MAX)
    return text or None


def destination_name(value: Any) -> str | None:
    """An outbound hook name a message is addressed to. ``None`` or empty means no destination."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not CHANNEL_RE.fullmatch(value):
        raise _fail(
            "destination must be the name of an outbound webhook",
            details={"field": "destination"},
        )
    return value


def origin(value: Any) -> dict[str, str] | None:
    """Where a message came from. Only ``discord`` today. Unknown keys are dropped."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _fail("origin must be an object", details={"field": "origin"})
    source = value.get("source")
    if source not in ORIGIN_SOURCES:
        raise _fail(
            f"origin.source must be one of {', '.join(ORIGIN_SOURCES)}", details={"field": "origin"}
        )
    clean = {"source": source, "channel_id": snowflake(value.get("channel_id"), "origin.channel_id")}
    message_id = optional_snowflake(value.get("message_id"), "origin.message_id")
    if message_id is not None:
        clean["message_id"] = message_id
    author = value.get("author")
    if author not in (None, ""):
        if not isinstance(author, str) or len(author) > ORIGIN_AUTHOR_MAX:
            raise _fail(
                f"origin.author must be a string of at most {ORIGIN_AUTHOR_MAX} characters",
                details={"field": "origin"},
            )
        clean["author"] = author.strip()
    return clean


def _attachment(value: Any, index: int) -> dict[str, Any]:
    field = f"attachments[{index}]"
    if not isinstance(value, dict):
        raise _fail(f"{field} must be an object", details={"field": field})
    filename = require_text(value.get("filename"), f"{field}.filename", FILENAME_MAX)
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise _fail(f"{field}.filename must not contain a path", details={"field": field})
    content_type = value.get("content_type") or "application/octet-stream"
    if not isinstance(content_type, str) or not CONTENT_TYPE_RE.fullmatch(content_type):
        raise _fail(f"{field}.content_type is not a media type", details={"field": field})
    size = value.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= ATTACHMENT_SIZE_MAX:
        raise _fail(
            f"{field}.size_bytes must be an integer 0..{ATTACHMENT_SIZE_MAX}",
            details={"field": field},
        )
    storage = value.get("storage")
    if storage not in ATTACHMENT_STORAGES:
        raise _fail(
            f"{field}.storage must be one of {', '.join(sorted(ATTACHMENT_STORAGES))}",
            details={"field": field},
        )
    bucket = value.get("bucket")
    if not isinstance(bucket, str) or not BUCKET_RE.fullmatch(bucket):
        raise _fail(f"{field}.bucket is not a valid bucket name", details={"field": field})
    object_key = require_text(value.get("object_key"), f"{field}.object_key", OBJECT_KEY_MAX)
    if object_key.startswith("/") or ".." in object_key.split("/"):
        raise _fail(f"{field}.object_key must be a relative key", details={"field": field})
    sha256 = value.get("sha256") or ""
    if not isinstance(sha256, str) or (sha256 and not SHA256_RE.fullmatch(sha256)):
        raise _fail(f"{field}.sha256 must be 64 lowercase hex characters", details={"field": field})
    return {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size,
        "storage": storage,
        "bucket": bucket,
        "object_key": object_key,
        "sha256": sha256,
    }


def attachments(value: Any) -> list[dict[str, Any]]:
    """Validated attachment descriptors. The hub records them; it never fetches the bytes."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail("attachments must be a list", details={"field": "attachments"})
    if len(value) > ATTACHMENTS_MAX:
        raise _fail(
            f"at most {ATTACHMENTS_MAX} attachments per message",
            details={"field": "attachments"},
        )
    return [_attachment(item, index) for index, item in enumerate(value)]


def client_request_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _fail("client_request_id must be a string")
    text = value.strip()
    if not text or len(text) > 200:
        raise _fail("client_request_id must be 1..200 characters")
    return text


def fingerprint(kind: str, payload: dict[str, Any]) -> str:
    """Stable hash of a creation payload. The idempotency key is not part of it."""
    encoded = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def task_status(value: Any) -> str:
    if not isinstance(value, str) or value not in TASK_STATUSES:
        raise _fail(
            "status must be one of open, claimed, in_progress, blocked, done, cancelled",
            details={"field": "status"},
        )
    return value


def transition_allowed(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())
