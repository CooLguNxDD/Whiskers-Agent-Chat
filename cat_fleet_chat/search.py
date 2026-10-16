"""Search input rules and result shaping. No SQL here: the FTS5 query lives in ``store.py``.

User text never reaches ``MATCH`` as written. :func:`build_match` keeps only
word tokens, quotes each one as an FTS5 string, and joins them with an
implicit AND, so quotes, ``*``, ``NEAR(``, ``AND``/``OR`` and column filters
in the input cannot form FTS5 syntax. A trailing ``*`` on a word is kept as a
prefix search on that word.
"""

from __future__ import annotations

import heapq
import re
import sys
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from cat_fleet_chat.errors import HubError

# Kinds the index holds. "knowledge" entries are active knowledge facts (S05).
SEARCH_KINDS = ("message", "task", "attachment", "knowledge")
SEARCH_MODES = ("bm25", "vector", "hybrid")
AVAILABLE_MODES = frozenset({"bm25"})

QUERY_MAX = 10_000
TERMS_MAX = 32
TERM_MAX = 64
LIMIT_DEFAULT = 20
LIMIT_MAX = 100
OFFSET_MAX = 10_000

# bm25 column weights: (title, body). A word in a task title or a filename
# outranks the same word somewhere in a long message.
BM25_WEIGHTS = (4.0, 1.0)
SNIPPET_MARKERS = ("[", "]", "…")
SNIPPET_TOKENS = 16

# Words FTS5 reads as operators when bare. Quoted they are ordinary terms, but
# a query of only these words is almost always an operator typed by habit.
_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})
# Letters and digits only. The unicode61 tokenizer splits on "_" too.
_TOKEN_RE = re.compile(r"([^\W_]+)(\*?)", re.UNICODE)
_RELATIVE_RE = re.compile(r"^\s*(\d{1,6})\s*([mhdw])\s*$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _fail(message: str, field: str) -> HubError:
    return HubError("validation_error", message, 400, {"field": field})


def query_text(value: Any) -> str:
    """The raw query string. Missing or non-text is a 400; empty text is allowed."""
    if not isinstance(value, str):
        raise _fail("q must be a string", "q")
    if len(value) > QUERY_MAX:
        raise _fail(f"q must be at most {QUERY_MAX} characters", "q")
    return value


def query_terms(value: str) -> list[tuple[str, bool]]:
    """Word tokens and whether each ends in ``*`` (prefix). At most :data:`TERMS_MAX`."""
    found = [(word[:TERM_MAX], bool(star) and len(word) <= TERM_MAX) for word, star in _TOKEN_RE.findall(value)]
    words = [item for item in found if item[0] not in _OPERATORS]
    return words[:TERMS_MAX]


def build_match(value: str) -> str | None:
    """A safe FTS5 MATCH expression for ``value``, or ``None`` when nothing is searchable."""
    terms = query_terms(value)
    if not terms:
        return None
    # \w never matches '"', but double it anyway so the quoting stays correct
    # if the token rule ever changes.
    return " ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' + ("*" if prefix else "") for word, prefix in terms)


def search_mode(value: Any, available: frozenset[str] = AVAILABLE_MODES) -> str:
    """The requested mode. ``available`` grows to every mode when an embedder is configured."""
    if value is None or value == "":
        return "bm25"
    mode = str(value).strip().lower()
    if mode not in SEARCH_MODES:
        raise _fail(f"mode must be one of {', '.join(SEARCH_MODES)}", "mode")
    if mode not in available:
        raise HubError(
            "not_available",
            f"search mode {mode} is not available: no embedder is configured "
            "(set CAT_FLEET_EMBED_PROVIDER, CAT_FLEET_EMBED_URL and CAT_FLEET_EMBED_MODEL); use bm25",
            400,
            {"mode": mode, "available": sorted(available)},
        )
    return mode


def search_kinds(value: Any) -> frozenset[str] | None:
    """One kind, a comma list, or a list. ``None`` means every kind."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise _fail("kind must be a string or a list", "kind")
    unknown = sorted(set(items) - set(SEARCH_KINDS))
    if unknown:
        raise _fail(
            f"unknown kind {', '.join(unknown)}; expected {', '.join(SEARCH_KINDS)}", "kind"
        )
    return frozenset(items) or None


def search_limit(value: Any) -> int:
    if value is None or value == "":
        return LIMIT_DEFAULT
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail("limit must be an integer", "limit") from exc
    if parsed < 1:
        raise _fail("limit must be at least 1", "limit")
    return min(parsed, LIMIT_MAX)


def search_offset(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise _fail("offset must be an integer", "offset") from exc
    if parsed < 0 or parsed > OFFSET_MAX:
        raise _fail(f"offset must be 0..{OFFSET_MAX}", "offset")
    return parsed


def search_since(value: Any, now: datetime | None = None) -> str | None:
    """An ISO timestamp, or a relative window (``30m``, ``24h``, ``7d``, ``2w``), as UTC ISO text.

    Stored ``created_at`` values are ``datetime.isoformat()`` in UTC, so the
    returned string compares correctly as text.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _fail("since must be an ISO timestamp or a window like 24h", "since")
    relative = _RELATIVE_RE.match(value)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2).lower()
        try:
            start = (now or datetime.now(timezone.utc)) - timedelta(**{_UNITS[unit]: amount})
        except OverflowError as exc:
            raise _fail("since window reaches before year 1", "since") from exc
        return start.astimezone(timezone.utc).isoformat()
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise _fail("since must be an ISO timestamp or a window like 24h", "since") from exc


def score(bm25_rank: float) -> float:
    """``bm25()`` is negative and lower-is-better; expose a positive higher-is-better score.

    Not rounded: FTS5 floors IDF at 1e-6, so a term found in most rows gives
    ranks near -1e-6 that rounding would collapse to 0. A non-negative rank
    maps to the smallest positive float so every hit keeps a positive score.
    """
    return max(-float(bm25_rank), sys.float_info.min)


def _with_knowledge(hit: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """A fact hit also names its ``project`` and ``topic``. Other kinds keep their shape."""
    if hit["kind"] == "knowledge":
        hit["project"] = row.get("project")
        hit["topic"] = row.get("topic")
    return hit


def shape_hit(row: dict[str, Any]) -> dict[str, Any]:
    hit = {
        "kind": row["kind"],
        "id": int(row["ref_id"]),
        "channel": row["channel"],
        "author": row["author"] or None,
        "created_at": row["created_at"],
        "snippet": row["snippet"],
        "score": score(row["bm25_rank"]),
    }
    return _with_knowledge(hit, row)


def shape_results(
    rows: list[dict[str, Any]], total: int, *, limit: int, offset: int, mode: str = "bm25"
) -> dict[str, Any]:
    end = offset + len(rows)
    return {
        "results": [shape_hit(row) for row in rows],
        "total_estimate": total,
        "mode": mode,
        "limit": limit,
        "offset": offset,
        "next_offset": end if end < total else None,
    }


def empty_results(*, limit: int, offset: int, mode: str = "bm25") -> dict[str, Any]:
    return shape_results([], 0, limit=limit, offset=offset, mode=mode)


# ---- vector and hybrid ranking ---------------------------------------------
#
# Hybrid search fuses the BM25 and vector rankings with reciprocal rank fusion
# (RRF): every list a hit appears in adds ``1 / (RRF_K + rank)``, rank from 1.
# RRF needs no score calibration between the two rankers.

RRF_K = 60
# Each ranker contributes at least this many candidates to the fusion.
FUSION_POOL_MIN = 50
# The query is embedded with at most this timeout, even if the provider's is longer.
QUERY_EMBED_TIMEOUT = 5.0


def fusion_pool(limit: int) -> int:
    """Candidates per ranker: ``max(limit*3, 50)``.

    Independent of ``offset`` so every page of one query pages through the same
    fused list (RRF scores do not shift between pages); hybrid hits end there.
    """
    return max(limit * 3, FUSION_POOL_MIN)


def top_k(scored: Iterable[tuple[float, Any]], k: int) -> list[tuple[float, Any]]:
    """The ``k`` best ``(score, item)`` pairs, best first, keeping only ``k`` in memory."""
    heap: list[tuple[float, int, Any]] = []
    for seq, (value, item) in enumerate(scored):
        entry = (value, -seq, item)
        if len(heap) < k:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)
    heap.sort(key=lambda entry: entry[:2], reverse=True)
    return [(value, item) for value, _, item in heap]


def rrf_fuse(rankings: Iterable[list[tuple[str, int]]], k: int = RRF_K) -> list[tuple[tuple[str, int], float]]:
    """Reciprocal rank fusion of ranked ``(kind, id)`` lists, de-duplicated, best first.

    Ties keep the order in which keys were first seen (the first ranking wins).
    """
    scores: dict[tuple[str, int], float] = {}
    for ranking in rankings:
        seen: set[tuple[str, int]] = set()
        for rank, key in enumerate(ranking, start=1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    order = {key: index for index, key in enumerate(scores)}
    return sorted(scores.items(), key=lambda item: (-item[1], order[item[0]]))


def plain_snippet(row: dict[str, Any]) -> str:
    """A snippet for a hit with no FTS5 highlight: the first words of the entry."""
    words = " ".join(part for part in (row.get("title") or "", row.get("body") or "") if part).split()
    if len(words) <= SNIPPET_TOKENS:
        return " ".join(words)
    return " ".join(words[:SNIPPET_TOKENS]) + SNIPPET_MARKERS[2]


def shape_vector_hit(row: dict[str, Any], value: float) -> dict[str, Any]:
    hit = {
        "kind": row["kind"],
        "id": int(row["ref_id"]),
        "channel": row["channel"],
        "author": row["author"] or None,
        "created_at": row["created_at"],
        "snippet": row.get("snippet") if row.get("snippet") is not None else plain_snippet(row),
        "score": float(value),
    }
    return _with_knowledge(hit, row)


def shape_scored(
    hits: list[dict[str, Any]], total: int, *, limit: int, offset: int, mode: str
) -> dict[str, Any]:
    """Result page for already shaped hits (vector, hybrid). Same keys as :func:`shape_results`."""
    end = offset + len(hits)
    return {
        "results": hits,
        "total_estimate": total,
        "mode": mode,
        "limit": limit,
        "offset": offset,
        "next_offset": end if end < total else None,
    }
