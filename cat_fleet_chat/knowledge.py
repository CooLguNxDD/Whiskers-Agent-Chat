"""Pure helpers for knowledge facts: validation, dedupe keys, LEARNED posts, JSONL lines.

No SQL and no business rules live here. ``store.py`` reads and writes
``knowledge_facts``; ``hub.py`` decides what is allowed.

The JSONL line format matches ``goal/<project>/_knowledge-inbox.jsonl``: one
JSON object per line with ``project``, ``topic`` and ``fact``, and optionally
``run``, ``step``, ``author`` and ``ts``. Unknown fields are ignored.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from cat_fleet_chat.errors import HubError
from cat_fleet_chat.feed import extract_hashtags
from cat_fleet_chat.validate import handle

FACT_MAX = 2_000
STATUSES = ("active", "superseded", "rejected")
DEFAULT_TOPIC = "general"
DEFAULT_PROJECT = "general"
# Lines per import call. Blank lines count too, so a caller cannot pad past it.
IMPORT_MAX = 5_000
# A single JSONL line longer than this is reported invalid without parsing.
LINE_MAX = 16_000
EVENT_SNIPPET = 120

# Projects are handle-like slugs; topics are short slugs. Both are stored lowercased.
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Run and step ids, e.g. "2026-10-15-2331-fleet-chat-v2" and "S05-knowledge".
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_LEARNED_RE = re.compile(r"^\s*learned\s*:", re.IGNORECASE)
_TOPIC_PREFIX_RE = re.compile(r"^\[\s*([A-Za-z0-9][A-Za-z0-9_-]{0,31})\s*\]\s*")
_TRAILING_PUNCT = ".,;:!?…。"
# "#run-<id>" / "#step-<id>" tags in a LEARNED post. Plain hashtags stop at "-",
# so these are read with their own pattern.
_SOURCE_TAG_RE = re.compile(r"(?<![\w#&/])#(run|step)-([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})", re.IGNORECASE)
_TRAILING_TAGS_RE = re.compile(r"(?:\s+#[\w.:-]+)+\s*$")


def _fail(message: str, field: str) -> HubError:
    return HubError("validation_error", message, 400, {"field": field})


def project_slug(value: Any, field: str = "project") -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not PROJECT_RE.fullmatch(text):
        raise _fail(f"{field} must match [a-z0-9][a-z0-9_-]{{0,63}}", field)
    return text


def optional_project(value: Any, field: str = "project") -> str | None:
    if value is None or value == "":
        return None
    return project_slug(value, field)


def topic_slug(value: Any, field: str = "topic") -> str:
    """A topic slug. Missing or empty means :data:`DEFAULT_TOPIC`."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_TOPIC
    text = value.strip().lower() if isinstance(value, str) else ""
    if not TOPIC_RE.fullmatch(text):
        raise _fail(f"{field} must match [a-z0-9][a-z0-9_-]{{0,31}}", field)
    return text


def optional_topic(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return topic_slug(value)


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


def source_id(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    text = value.strip() if isinstance(value, str) else ""
    if not SOURCE_RE.fullmatch(text):
        raise _fail(f"{field} must be 1..128 letters, digits, '_', '.', ':' or '-'", field)
    return text


def fact_text(value: Any) -> str:
    """NFC text with ``\\n`` line endings and no surrounding whitespace, 1..2000 characters."""
    if not isinstance(value, str):
        raise _fail("fact must be a string", "fact")
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not text:
        raise _fail("fact is required", "fact")
    if len(text) > FACT_MAX:
        raise _fail(f"fact exceeds {FACT_MAX} characters", "fact")
    return text


def normalize_fact(text: str) -> str:
    """The dedupe key: NFKC, casefolded, whitespace collapsed, trailing punctuation dropped."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    collapsed = " ".join(folded.split())
    return collapsed.rstrip(_TRAILING_PUNCT + " ").strip()


def parse_learned(body: Any) -> tuple[str | None, str] | None:
    """``(topic, fact)`` for a post whose first non-blank line starts with ``LEARNED:``.

    The prefix is case-insensitive. ``LEARNED: [sqlite] text`` sets the topic.
    The fact is the rest of the first line plus any lines after it. Returns
    ``None`` when the body is not a LEARNED post or the fact is empty.
    """
    if not isinstance(body, str):
        return None
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _LEARNED_RE.match(line)
        if match is None:
            return None
        rest = "\n".join([line[match.end():], *lines[index + 1:]]).strip()
        topic = None
        prefix = _TOPIC_PREFIX_RE.match(rest)
        if prefix is not None:
            topic = prefix.group(1).lower()
            rest = rest[prefix.end():].strip()
        return (topic, rest) if rest else None
    return None


def learned_from_post(post: dict[str, Any]) -> dict[str, Any] | None:
    """The fact a feed post teaches, or ``None``.

    ``project`` is the post's first hashtag (when it is a valid project slug),
    else ``general``. ``#run-<id>`` / ``#step-<id>`` tags set the source, and
    hashtags trailing the fact are dropped from its text.
    """
    if post.get("deleted"):
        return None
    body = post.get("body") or ""
    parsed = parse_learned(body)
    if parsed is None:
        return None
    topic, text = parsed
    sources: dict[str, str] = {}
    for kind, value in _SOURCE_TAG_RE.findall(body):
        sources.setdefault(kind.lower(), value)
    text = _SOURCE_TAG_RE.sub(" ", text)
    text = _TRAILING_TAGS_RE.sub("", text).strip()
    if not text:
        return None
    project = DEFAULT_PROJECT
    source_tags = {"run", "step"} if sources else set()
    for tag in post.get("hashtags") or extract_hashtags(body):
        if tag in source_tags:
            continue
        if PROJECT_RE.fullmatch(tag):
            project = tag
        break
    return {
        "project": project,
        "topic": topic,
        "fact": text[:FACT_MAX],
        "source_run": sources.get("run"),
        "source_step": sources.get("step"),
    }


def shape_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project": row["project"],
        "topic": row["topic"],
        "fact": row["fact"],
        "source_run": row["source_run"],
        "source_step": row["source_step"],
        "author": row["author"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def event_payload(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fact["id"],
        "project": fact["project"],
        "topic": fact["topic"],
        "author": fact["author"],
        "fact": fact["fact"][:EVENT_SNIPPET],
    }


def _timestamp(value: Any) -> str | None:
    """A JSONL ``ts`` as UTC ISO text, or ``None`` when it is missing or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc).isoformat()
    except OverflowError:
        return None


def parse_line(line: Any) -> dict[str, Any]:
    """One JSONL line as clean fact fields. Raises ``HubError`` for a bad line.

    ``author`` is ``None`` when the line has none; the importer fills it in.
    """
    if not isinstance(line, str):
        raise _fail("line must be a string", "line")
    if len(line) > LINE_MAX:
        raise _fail(f"line exceeds {LINE_MAX} characters", "line")
    try:
        item = json.loads(line)
    except json.JSONDecodeError as exc:
        raise _fail(f"line is not JSON ({exc.msg})", "line") from None
    except ValueError:  # e.g. an integer past the int digit limit
        raise _fail("line is not JSON (number too large)", "line") from None
    except RecursionError:
        raise _fail("line is not JSON (nested too deeply)", "line") from None
    if not isinstance(item, dict):
        raise _fail("line must be a JSON object", "line")
    author = item.get("author")
    author = handle(author, "author") if author not in (None, "") else None
    return {
        "project": project_slug(item.get("project")),
        "topic": topic_slug(item.get("topic")),
        "fact": fact_text(item.get("fact")),
        "source_run": source_id(item.get("run"), "run"),
        "source_step": source_id(item.get("step"), "step"),
        "author": author,
        "created_at": _timestamp(item.get("ts")),
    }


def import_lines(value: Any) -> list[str]:
    """JSONL text or a list of lines, as a list of lines. At most :data:`IMPORT_MAX`."""
    if isinstance(value, str):
        lines = value.splitlines()
    elif isinstance(value, (list, tuple)):
        lines = list(value)
    else:
        raise _fail("lines must be JSONL text or a list of lines", "lines")
    if len(lines) > IMPORT_MAX:
        raise HubError(
            "validation_error",
            f"an import takes at most {IMPORT_MAX} lines; split the file",
            400,
            {"field": "lines", "max": IMPORT_MAX, "got": len(lines)},
        )
    return lines


def serialize_fact(fact: dict[str, Any]) -> str:
    """One export line, in the inbox shape. ``run``/``step`` are left out when unset."""
    item: dict[str, Any] = {"project": fact["project"], "topic": fact["topic"], "fact": fact["fact"]}
    if fact.get("source_run"):
        item["run"] = fact["source_run"]
    if fact.get("source_step"):
        item["step"] = fact["source_step"]
    item["author"] = fact["author"]
    item["ts"] = fact["created_at"]
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
