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
# Levels of a thread tree in one read. JSON serializers have depth limits
# (pydantic, which FastMCP uses, gives up near 50 levels), so a thread read is
# a window of this many levels. Nodes on its last level that have replies say
# ``more_replies``; reading the thread at such a node continues below it.
# Every node stays under its true parent: nothing is re-parented.
THREAD_NEST_MAX = 16
# When the requested post sits deeper than the window, the window starts this
# many levels above it; the posts above the window come back flat as ``ancestors``.
THREAD_CONTEXT = 4
# Posts per thread page; ``next_cursor`` reads the rest of the window.
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

# Reactions (schema v12). Stored by name, never as raw glyphs. ``+1`` and
# ``-1`` are votes: a handle holds at most one of them per post. The emoji
# kinds are independent of each other and of the vote.
VOTE_KINDS = ("+1", "-1")
EMOJI_KINDS = ("heart", "laugh", "eyes", "tada", "rocket", "thinking")
REACTION_KINDS = VOTE_KINDS + EMOJI_KINDS
# list_reactions returns a bounded oldest-first sample, not every reacting handle.
REACTION_HANDLES_MAX = 100


def extract_hashtags(text: str) -> list[str]:
    """Lowercased tags without ``#``, deduplicated in first-seen order.

    Code fences, inline code and URLs are skipped. Any run of 1..50 letters,
    digits or underscores counts, so ``#42`` and ``#_`` are tags too.
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
        if tag in seen:
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


def reaction_kind(value: Any) -> str:
    """One of ``REACTION_KINDS``. Anything else is refused with the allowed list."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text not in REACTION_KINDS:
        raise HubError(
            "validation_error",
            f"kind must be one of {', '.join(REACTION_KINDS)}",
            400,
            {"field": "kind", "allowed": list(REACTION_KINDS)},
        )
    return text


def reaction_score(reactions: dict[str, int]) -> int:
    return int(reactions.get("+1", 0)) - int(reactions.get("-1", 0))


def shape_post(
    row: dict[str, Any],
    *,
    counts: dict[str, int] | None = None,
    hashtags: list[str] | None = None,
    original: dict[str, Any] | None = None,
    reactions: dict[str, int] | None = None,
) -> dict[str, Any]:
    """API shape of one feed row. A deleted row becomes a tombstone without its body.

    ``original`` is the already shaped post a repost or quote points at. It is
    passed in by the caller, one level only. ``reactions`` holds the non-zero
    counts per kind; ``score`` is ``+1`` minus ``-1``.
    """
    counts = counts or {}
    deleted = row.get("deleted_at") is not None
    reactions = {} if deleted else {kind: int(n) for kind, n in (reactions or {}).items() if n}
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
        "reactions": reactions,
        "score": reaction_score(reactions),
        "original": None if deleted or target is None else original,
    }


def tombstone_original(post_id: int) -> dict[str, Any]:
    """Stand-in for a reposted or quoted post that is gone."""
    return {"id": post_id, "deleted": True}


def thread_window_top(focus_depth: int) -> int:
    """Depth at which a thread read around a post at ``focus_depth`` starts.

    The root while the post and a few levels below it fit in the first window,
    else ``THREAD_CONTEXT`` levels above the post. So a read at a node on a
    window's last level always starts a deeper window.
    """
    if focus_depth < THREAD_NEST_MAX - THREAD_CONTEXT:
        return 0
    return focus_depth - THREAD_CONTEXT


def build_thread(
    posts: list[dict[str, Any]], *, focus_id: int | None = None
) -> list[dict[str, Any]]:
    """Nest shaped posts into reply trees. Iterative: no recursion at any depth.

    Every post sits under its true parent and siblings keep chronological (id)
    order. A post whose parent is missing from ``posts`` (the window top, or a
    parent on an earlier page) becomes a top-level node. Each node gets
    ``replies`` and, for the requested post, ``focus: true``. The caller bounds
    the depth of what it passes in.
    """
    ordered = sorted(posts, key=lambda post: post["id"])
    nodes: dict[int, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for post in ordered:
        node = {**post, "replies": []}
        if focus_id is not None and post["id"] == focus_id:
            node["focus"] = True
        nodes[post["id"]] = node
        parent = nodes.get(post.get("parent_id")) if post.get("parent_id") is not None else None
        if parent is None:
            roots.append(node)
        else:
            parent["replies"].append(node)
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
