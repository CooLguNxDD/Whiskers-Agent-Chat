"""Pure helpers for the agent feed: hashtags, bodies, cursors, post shapes, thread trees.

No SQL and no business rules live here. ``store.py`` reads and writes the feed
tables; ``hub.py`` decides what is allowed.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from typing import Any

from cat_fleet_chat.errors import HubError

BODY_MAX = 4_000
TAG_MAX = 50
# How deep a reply may sit below its root. A reply past this is refused.
DEPTH_MAX = 1_000
# Display nesting of a thread tree. Replies deeper than this attach to their
# ancestor at the cap (``parent_id``/``depth`` stay true). JSON serializers
# have depth limits (pydantic, which FastMCP uses, gives up near 50 levels),
# so an unbounded tree cannot leave the hub.
THREAD_NEST_MAX = 16
# A thread read returns at most this many posts and says ``truncated``.
THREAD_ROWS_MAX = 5_000
TIMELINE_SCOPES = ("global", "following", "hashtag", "author", "thread")
TRENDING_DEFAULT = 10
TRENDING_MAX = 50
TRENDING_WINDOW_HOURS = 24

# A hashtag is "#" plus 1..50 unicode letters, digits or underscores, not glued
# to a preceding word, "#", "&" (HTML entities) or "/" (URL fragments), and not
# running on past 50 characters.
HASHTAG_RE = re.compile(r"(?<![\w#&/])#(\w{1,%d})(?!\w)" % TAG_MAX)
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+|\bwww\.\S+")

_CURSOR_PREFIX = "v1:"


def extract_hashtags(text: str) -> list[str]:
    """Lowercased tags without ``#``, deduplicated in first-seen order.

    Code fences, inline code and URLs are skipped. Tags made only of digits
    (``#42``) are issue references, not hashtags.
    """
    if not text:
        return []
    cleaned = _FENCE_RE.sub(" ", text)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    seen: set[str] = set()
    found: list[str] = []
    for match in HASHTAG_RE.finditer(cleaned):
        tag = match.group(1).lower()
        if tag.isdigit() or tag.strip("_") == "" or tag in seen:
            continue
        seen.add(tag)
        found.append(tag)
    return found


def normalize_body(value: Any, *, required: bool = True) -> str:
    """NFC text with ``\\n`` line endings and no surrounding whitespace, at most 4000 characters."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HubError("validation_error", "body must be a string", 400, {"field": "body"})
    text = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if required and not text:
        raise HubError("validation_error", "body is required", 400, {"field": "body"})
    if len(text) > BODY_MAX:
        raise HubError(
            "validation_error", f"body exceeds {BODY_MAX} characters", 400, {"field": "body"}
        )
    return text


def encode_cursor(post_id: int) -> str:
    """Opaque page cursor for "older than ``post_id``"."""
    raw = f"{_CURSOR_PREFIX}{int(post_id)}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: Any) -> int | None:
    """The post id inside a cursor. ``None`` or empty means the first page."""
    if value is None or value == "":
        return None
    error = HubError("validation_error", "cursor is not valid", 400, {"field": "cursor"})
    if not isinstance(value, str) or len(value) > 64:
        raise error
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except (binascii.Error, ValueError) as exc:
        raise error from exc
    if not raw.startswith(_CURSOR_PREFIX) or not raw[len(_CURSOR_PREFIX):].isdigit():
        raise error
    parsed = int(raw[len(_CURSOR_PREFIX):])
    if parsed < 1:
        raise error
    return parsed


def post_kind(row: dict[str, Any]) -> str:
    if row.get("repost_of") is not None:
        return "repost"
    if row.get("quote_of") is not None:
        return "quote"
    if row.get("parent_id") is not None:
        return "reply"
    return "post"


def shape_post(
    row: dict[str, Any],
    *,
    counts: dict[str, int] | None = None,
    hashtags: list[str] | None = None,
    original: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """API shape of one feed row. A deleted row becomes a tombstone without its body.

    ``original`` is the already shaped post a repost or quote points at. It is
    passed in by the caller, one level only.
    """
    counts = counts or {}
    deleted = row.get("deleted_at") is not None
    target = row.get("repost_of") or row.get("quote_of")
    return {
        "id": row["id"],
        "kind": post_kind(row),
        "author": row["author"],
        "body": "" if deleted else row["body"],
        "parent_id": row.get("parent_id"),
        "root_id": row.get("root_id"),
        "depth": row.get("depth", 0),
        "repost_of": row.get("repost_of"),
        "quote_of": row.get("quote_of"),
        "created_at": row["created_at"],
        "deleted": deleted,
        "deleted_at": row.get("deleted_at"),
        "hashtags": [] if deleted else list(hashtags or []),
        "reply_count": int(counts.get("reply_count", 0)),
        "repost_count": int(counts.get("repost_count", 0)),
        "quote_count": int(counts.get("quote_count", 0)),
        "original": None if deleted or target is None else original,
    }


def tombstone_original(post_id: int) -> dict[str, Any]:
    """Stand-in for a reposted or quoted post that is gone."""
    return {"id": post_id, "deleted": True}


def build_thread(
    posts: list[dict[str, Any]],
    *,
    focus_id: int | None = None,
    max_nesting: int | None = THREAD_NEST_MAX,
) -> list[dict[str, Any]]:
    """Nest shaped posts into reply trees. Iterative: no recursion at any depth.

    Siblings keep chronological (id) order. A post whose parent is missing from
    ``posts`` becomes a top-level node, so a truncated thread still renders.
    With ``max_nesting`` a node never sits more than that many levels below its
    top-level node; deeper replies join their ancestor's replies at the cap.
    Each node gets ``replies`` and, for the requested post, ``focus: true``.
    """
    ordered = sorted(posts, key=lambda post: post["id"])
    nodes: dict[int, dict[str, Any]] = {}
    level: dict[int, int] = {}
    roots: list[dict[str, Any]] = []
    for post in ordered:
        node = {**post, "replies": []}
        if focus_id is not None and post["id"] == focus_id:
            node["focus"] = True
        nodes[post["id"]] = node
        parent = nodes.get(post.get("parent_id")) if post.get("parent_id") is not None else None
        if parent is None:
            level[post["id"]] = 0
            roots.append(node)
            continue
        holder = parent
        if max_nesting is not None:
            # Climb to the ancestor sitting one level above the cap. Top-level
            # nodes are at level 0, so the climb always ends.
            cap = max(1, max_nesting)
            while level[holder["id"]] >= cap:
                holder = nodes[holder["_holder"]]
        node["_holder"] = holder["id"]
        level[post["id"]] = level[holder["id"]] + 1
        holder["replies"].append(node)
    for node in nodes.values():
        node.pop("_holder", None)
    return roots


def iter_tree(roots: list[dict[str, Any]]):
    """Depth-first walk of a thread tree without recursion. Yields every node."""
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("replies") or []))


def clean_tag(value: Any) -> str:
    """A tag filter: optional leading ``#``, lowercased, same characters as a hashtag."""
    text = value.strip().lstrip("#").lower() if isinstance(value, str) else ""
    if not re.fullmatch(r"\w{1,%d}" % TAG_MAX, text):
        raise HubError(
            "validation_error",
            f"tag must be 1..{TAG_MAX} letters, digits or underscores",
            400,
            {"field": "tag"},
        )
    return text


def event_snippet(body: str, limit: int = 120) -> str:
    """The first ``limit`` characters of a body, for event payloads."""
    return body[:limit]
