"""Platform-neutral inbound routing: map entries, ``#channel`` targeting and author handles.

Shared by every relay platform and by the legacy ``cat-fleet-discord``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cat_fleet_chat.validate import CHANNEL_RE

DEFAULT_PREFIX = "dc-"


@dataclass(frozen=True)
class MapEntry:
    """Where one Discord channel may post in the hub.

    ``default`` takes every message with no ``#channel`` token. A token may
    only pick a channel in ``allowed``, or any channel when ``any_channel`` is
    set, so a Discord channel cannot reach hub channels you did not list.
    """

    default: str
    allowed: frozenset[str]
    any_channel: bool = False


def parse_map(items: list[str]) -> dict[int, MapEntry]:
    """``["123=fleet,ops", "456=*"]`` into per-channel entries. The first name is the default.

    ``*`` lets a ``#token`` pick any hub channel. Rejects malformed entries.
    """
    mapping: dict[int, MapEntry] = {}
    for item in items:
        left, sep, right = item.partition("=")
        names = [part.strip() for part in right.split(",") if part.strip()]
        valid = sep and left.strip().isdigit() and names
        if not valid or not CHANNEL_RE.fullmatch(names[0]):
            raise ValueError(
                f"--map expects <discord_channel_id>=<hub_channel>[,<more channels>|*], got {item!r}"
            )
        any_channel = "*" in names[1:]
        extras = [name for name in names[1:] if name != "*"]
        if not all(CHANNEL_RE.fullmatch(name) for name in extras):
            raise ValueError(f"--map has an invalid hub channel name in {item!r}")
        mapping[int(left.strip())] = MapEntry(names[0], frozenset([names[0], *extras]), any_channel)
    if not mapping:
        raise ValueError("at least one --map is required")
    return mapping


# A hub channel written as #name, standing alone: not part of a word, a URL
# fragment or another token.
TARGET_RE = re.compile(r"(?<![\w#/])#([a-z0-9][a-z0-9_-]{0,63})(?![\w-])")


def parse_target(
    text: str, entry: MapEntry, known: frozenset[str] | set[str] | None
) -> tuple[str, str, str | None]:
    """Pick the hub channel for ``text``. Returns ``(channel, text, note)``.

    The first ``#name`` that is a hub channel becomes the target and is removed
    from the text. A ``#name`` the hub does not have is left alone (it may be a
    Discord channel). A hub channel this Discord channel may not reach keeps
    the default and returns a note. With no channel list (``known`` is None)
    nothing is targeted.
    """
    if known is None:
        return entry.default, text, None
    for match in TARGET_RE.finditer(text):
        name = match.group(1)
        if name not in known:
            continue
        if not (entry.any_channel or name in entry.allowed):
            return entry.default, text, f"#{name} is not allowed from this Discord channel"
        cleaned = re.sub(r"[ \t]{2,}", " ", text[: match.start()] + text[match.end() :]).strip()
        return name, cleaned or text, None
    return entry.default, text, None


def author_handle(display_name: str, prefix: str = DEFAULT_PREFIX) -> str:
    """A hub handle for a Discord user. Always starts with ``prefix`` so hooks can filter on it."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_")
    return (prefix + (slug or "user"))[:64].rstrip("-_")
