"""Relay control plane: pure rules for relays, bindings, routes and their config.

A **relay** is a chat-platform bridge process (``cat-fleet-relay``) the hub
knows by name: Discord, Slack or Telegram. Its config, its **bindings**
(platform channel or thread <-> hub channel) and its **routes** (a durable
consumer delivered to a platform channel) live in the hub, so they change
live from REST, MCP or the portal and every change is audited.

Validation, shaping and the reaction grammar only; SQL lives in ``store.py``
and the rules in ``relay_hub.py``. Raises ``HubError`` for API input and
``ValueError`` for the relay's own env/CLI parsing.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from cat_fleet_chat.consumers import scrub
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.evolve import parse_command
from cat_fleet_chat.validate import CHANNEL_RE

PLATFORMS = ("discord", "slack", "telegram")
# Short, lowercase: it is also a handle fragment and a ``relay:<name>`` destination.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Platform channel, thread, chat or user ids: Discord snowflakes, Slack
# ``C0123``/``1700000000.000100``, Telegram ``-100123`` or ``-100123:7``.
EXTERNAL_ID_RE = re.compile(r"^-?[A-Za-z0-9][A-Za-z0-9_:.-]{0,63}$")
FORMATS = ("line", "embed", "card")
BINDING_KINDS = ("channel", "thread")
DIRECTIONS = ("in", "out", "both")
READ_COMMAND_POLICIES = ("bound", "operators")
DEFAULT_PREFIX = {"discord": "dc-", "slack": "sl-", "telegram": "tg-"}
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,15}$")
DESTINATION_PREFIX = "relay:"
DESCRIPTION_MAX = 500
# Ids in one allowlist, bindings and routes per relay.
LIST_MAX = 100
BINDINGS_MAX = 200
ROUTES_MAX = 100
STATUS_MAX = 4_000
# A relay that heartbeated this recently is online; up to STALE it is stale.
ONLINE_SECONDS = 45
STALE_SECONDS = 300
TOKEN_PREFIX = "rly_"

# ---- reaction grammar (shared with cat-fleet-discord) ----------------------

# Delivery marks used instead when ✅/❌ are reaction actions, so a delivered
# message never reads as both approved and rejected. Reserved: never actions.
OK_REACTION = "✅"
FAIL_REACTION = "❌"
ALT_OK_REACTION = "📨"
ALT_FAIL_REACTION = "⚠️"
DEFAULT_REACTIONS = {
    "✅": "approve {P}", "❌": "reject {P} via Discord reaction",
    "⏸️": "pause evolve", "▶️": "resume evolve",
    **{chr(0x1F1E6 + n): "{step} answer: (" + chr(97 + n) + ")" for n in range(5)},
    "👍": "+1", "👎": "-1",
}
ANSWER_RE = re.compile(r"\{step\} answer: \(([a-e])\)")
MAX_ACTION_REACTIONS = 12


def bare(emoji: str) -> str:
    """Discord may omit the optional variation selector (pause/play, warning)."""
    return emoji.replace("️", "")


def check_reaction(emoji: str, template: str, existing: dict[str, str]) -> None:
    """Raise ``ValueError`` unless ``emoji -> template`` is a valid new action.

    Values are evolve commands with ``{P}``, pause/resume, ``{step} answer:
    (a)`` .. ``(e)``, or feed kinds ``+1`` / ``-1``. Literal proposal ids,
    unknown placeholders, duplicates and the reserved delivery marks are
    errors.
    """
    command = parse_command(template.replace("{P}", "P1"))
    valid_command = command is not None and (
        command.number is None or (
            re.match(r"[/!]?(approve|reject|edit)[ \t]+\{P\}(?:[ \t]|$)", template, re.IGNORECASE)
            and template.count("{P}") == 1 and "{step}" not in template
        )
    )
    valid = template in {"+1", "-1"} or ANSWER_RE.fullmatch(template) or valid_command
    # Only supported literal placeholders; str.format conversions and
    # attribute/index traversal are deliberately not part of this grammar.
    remainder = template.replace("{P}", "").replace("{step}", "")
    if (not emoji or len(emoji) > 16 or not any(ord(c) > 0x2000 for c in emoji)
            or any(c.isspace() for c in emoji)
            or bare(emoji) in {bare(ALT_OK_REACTION), bare(ALT_FAIL_REACTION)}
            or any(bare(key) == bare(emoji) for key in existing)
            or not valid or "{" in remainder or "}" in remainder
            or "\n" in template or "\r" in template):
        raise ValueError(f"invalid reaction action {emoji!r}={template!r}")


def reaction_map(value: Any) -> dict[str, str]:
    """A reaction action map from the API; ``None`` keeps the defaults."""
    if value is None:
        return dict(DEFAULT_REACTIONS)
    if not isinstance(value, dict):
        raise _fail("reactions must be an object of emoji -> action", "config.reactions")
    result: dict[str, str] = {}
    for emoji, template in value.items():
        if not isinstance(template, str):
            raise _fail("reaction actions must be strings", "config.reactions")
        try:
            check_reaction(str(emoji).strip(), template.strip(), result)
        except ValueError as exc:
            raise _fail(str(exc), "config.reactions") from exc
        result[str(emoji).strip()] = template.strip()
    if len(result) > MAX_ACTION_REACTIONS:
        raise _fail(f"at most {MAX_ACTION_REACTIONS} reaction actions", "config.reactions")
    return result


# ---- validation ------------------------------------------------------------


def _fail(message: str, field: str) -> HubError:
    return HubError("validation_error", message, 400, {"field": field})


def name(value: Any, field: str = "name") -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value):
        raise _fail(f"{field} must match [a-z0-9][a-z0-9_-]{{0,39}}", field)
    return value


def platform(value: Any) -> str:
    if value not in PLATFORMS:
        raise HubError(
            "validation_error",
            f"platform must be one of {', '.join(PLATFORMS)}",
            400,
            {"field": "platform", "allowed": list(PLATFORMS)},
        )
    return value


def external_id(value: Any, field: str) -> str:
    text = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not EXTERNAL_ID_RE.fullmatch(text):
        raise _fail(f"{field} must be a platform id (letters, digits, -_:.)", field)
    return text


def optional_external_id(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return external_id(value, field)


def _ids(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise _fail(f"{field} must be a list of ids", field)
    if len(value) > LIST_MAX:
        raise _fail(f"{field} holds at most {LIST_MAX} ids", field)
    out: list[str] = []
    for item in value:
        clean = external_id(item, field)
        if clean not in out:
            out.append(clean)
    return out


def _flag(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _fail(f"{field} must be true or false", field)
    return value


def description(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > DESCRIPTION_MAX:
        raise _fail(f"description must be at most {DESCRIPTION_MAX} characters", "description")
    return value.strip() or None


CONFIG_KEYS = (
    "prefix",
    "operators",
    "action_users",
    "reactions",
    "ref_webhooks",
    "guilds",
    "mask",
    "status_marks",
    "read_commands",
)


def config(value: Any, relay_platform: str) -> dict[str, Any]:
    """A relay's full config, with defaults filled in. Unknown keys are rejected.

    - ``prefix``: handle prefix for relayed authors (``dc-``, ``sl-``, ``tg-``)
    - ``operators``: platform user ids allowed to run write commands
    - ``action_users``: platform user ids whose reactions and buttons act
    - ``reactions``: emoji -> action (``check_reaction``); null keeps defaults
    - ``ref_webhooks``: Discord webhook ids whose ``cat-fleet-ref:`` footers are trusted
    - ``guilds``: Discord guild ids to sync slash commands to (empty: global)
    - ``mask``: mask credential shapes in relayed text (default on)
    - ``status_marks``: react with delivery marks (default on)
    - ``read_commands``: ``bound`` (anyone in a bound channel) or ``operators``
    """
    raw = {} if value is None else value
    if not isinstance(raw, dict):
        raise _fail("config must be an object", "config")
    unknown = sorted(set(raw) - set(CONFIG_KEYS))
    if unknown:
        raise HubError(
            "validation_error",
            "unknown config key: " + ", ".join(unknown),
            400,
            {"field": "config", "allowed": list(CONFIG_KEYS)},
        )
    prefix = raw.get("prefix") or DEFAULT_PREFIX[relay_platform]
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        raise _fail("prefix must be 1..16 letters, digits, '_' or '-'", "config.prefix")
    policy = raw.get("read_commands") or "bound"
    if policy not in READ_COMMAND_POLICIES:
        raise _fail(f"read_commands must be one of {', '.join(READ_COMMAND_POLICIES)}", "config.read_commands")
    return {
        "prefix": prefix,
        "operators": _ids(raw.get("operators"), "config.operators"),
        "action_users": _ids(raw.get("action_users"), "config.action_users"),
        "reactions": reaction_map(raw.get("reactions")),
        "ref_webhooks": _ids(raw.get("ref_webhooks"), "config.ref_webhooks"),
        "guilds": _ids(raw.get("guilds"), "config.guilds"),
        "mask": _flag(raw.get("mask"), "config.mask", True),
        "status_marks": _flag(raw.get("status_marks"), "config.status_marks", True),
        "read_commands": policy,
    }


def merge_config(current: dict[str, Any], patch: Any, relay_platform: str) -> dict[str, Any]:
    """A PATCH: top-level keys in ``patch`` replace the current ones, then the result is revalidated."""
    if not isinstance(patch, dict):
        raise _fail("config must be an object", "config")
    return config({**current, **patch}, relay_platform)


def hub_channels(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > LIST_MAX:
        raise _fail(f"{field} must be a list of channel names", field)
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not CHANNEL_RE.fullmatch(item):
            raise _fail(f"{field} must be a list of channel names", field)
        if item not in out:
            out.append(item)
    return out


def binding(body: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Clean a binding. ``external_id`` is the platform channel or thread.

    ``hub_channel`` takes every inbound message; ``allowed`` lists the hub
    channels a ``#channel`` token may pick (``any_channel`` lifts the limit).
    ``task_id`` ties a thread to one task. ``direction`` says whether the
    binding relays inbound, outbound or both.
    """
    out: dict[str, Any] = {}

    def has(key: str) -> bool:
        return not partial or key in body

    if has("external_id"):
        out["external_id"] = external_id(body.get("external_id"), "external_id")
    if has("hub_channel"):
        value = body.get("hub_channel")
        if not isinstance(value, str) or not CHANNEL_RE.fullmatch(value):
            raise _fail("hub_channel must be a channel name", "hub_channel")
        out["hub_channel"] = value
    if has("kind"):
        kind = body.get("kind") or "channel"
        if kind not in BINDING_KINDS:
            raise _fail(f"kind must be one of {', '.join(BINDING_KINDS)}", "kind")
        out["kind"] = kind
    if has("parent_external_id"):
        out["parent_external_id"] = optional_external_id(body.get("parent_external_id"), "parent_external_id")
    if has("allowed"):
        out["allowed"] = hub_channels(body.get("allowed"), "allowed")
    if has("any_channel"):
        out["any_channel"] = _flag(body.get("any_channel"), "any_channel", False)
    if has("task_id"):
        task = body.get("task_id")
        if task in (None, ""):
            out["task_id"] = None
        elif isinstance(task, bool) or not isinstance(task, int) or task < 1:
            raise _fail("task_id must be a positive integer", "task_id")
        else:
            out["task_id"] = task
    if has("direction"):
        direction = body.get("direction") or "both"
        if direction not in DIRECTIONS:
            raise _fail(f"direction must be one of {', '.join(DIRECTIONS)}", "direction")
        out["direction"] = direction
    if out.get("kind") == "thread" and not partial and out.get("parent_external_id") is None:
        raise _fail("a thread binding needs parent_external_id", "parent_external_id")
    return out


def route_format(value: Any) -> str:
    fmt = value or "line"
    if fmt not in FORMATS:
        raise _fail(f"format must be one of {', '.join(FORMATS)}", "format")
    return fmt


def status(value: Any) -> dict[str, Any]:
    """A heartbeat's status object, scrubbed of secret-looking keys and capped."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail("status must be an object", "status")
    clean = scrub(value)
    if len(json.dumps(clean, default=str)) > STATUS_MAX:
        raise _fail(f"status exceeds {STATUS_MAX} characters as JSON", "status")
    return clean


def expected_version(value: Any, *, required: bool) -> int | None:
    """``If-Match`` / ``expected_version``: the version the caller reviewed."""
    if value in (None, ""):
        if required:
            raise HubError(
                "precondition_required",
                "pass the relay version you reviewed (If-Match or expected_version)",
                428,
                {"field": "expected_version"},
            )
        return None
    text = str(value).strip().strip('"')
    if not text.isdigit():
        raise _fail("expected_version must be an integer", "expected_version")
    return int(text)


# ---- tokens ----------------------------------------------------------------


def new_token() -> str:
    """A relay token. Returned once; only its hash is stored."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---- shaping ---------------------------------------------------------------


def liveness(last_seen_at: str | None, now: datetime | None = None) -> str:
    """``online``, ``stale`` or ``offline`` from the last heartbeat."""
    if not last_seen_at:
        return "offline"
    try:
        seen = datetime.fromisoformat(last_seen_at)
    except ValueError:
        return "offline"
    age = ((now or datetime.now(timezone.utc)) - seen).total_seconds()
    if age <= ONLINE_SECONDS:
        return "online"
    if age <= STALE_SECONDS:
        return "stale"
    return "offline"


def public_relay(relay: dict[str, Any]) -> dict[str, Any]:
    """A relay as the API shows it: no token hash, plus ``has_token`` and ``state``."""
    out = {key: value for key, value in relay.items() if key != "token_hash"}
    out["has_token"] = bool(relay.get("token_hash"))
    out["state"] = liveness(relay.get("last_seen_at")) if relay.get("enabled") else "disabled"
    return out


def destination(relay_name: str) -> str:
    return DESTINATION_PREFIX + relay_name


def replies_consumer(relay_name: str) -> str:
    """The built-in consumer that delivers replies addressed to this relay."""
    return f"relay-{relay_name}-replies"


def route_consumer(relay_name: str, suffix: str) -> str:
    """The consumer a route delivers from; ``suffix`` keeps names unique per relay."""
    return f"relay-{relay_name}-{suffix}"


def event_payload(relay: dict[str, Any], actor: str, change: str) -> dict[str, Any]:
    return {
        "name": relay["name"],
        "platform": relay["platform"],
        "version": relay["version"],
        "actor": actor,
        "change": change,
    }
