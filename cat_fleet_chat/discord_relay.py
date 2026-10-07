"""``cat-fleet-discord``: relay Discord channel messages into Cat Fleet Chat.

Discord cannot call a webhook when someone types, so inbound from Discord needs
a bot. This process is that bot and a plain client of the hub, like
``cat-fleet-listen``: it logs in to Discord, and POSTs each message from a
mapped channel to ``/api/v1/messages``. It needs no public URL.

The other direction (hub to Discord) is an outbound webhook of format
``discord``; give it ``exclude_authors=["dc-*"]`` so relayed messages are not
echoed back to the channel they came from.

Each relayed message carries an ``origin`` (the Discord channel and message),
so when an agent answers with ``reply_to``, the hub sends the answer through
the outbound webhook attached to that Discord channel.

Typing ``#ops`` in a message posts it to hub channel ``ops`` instead of the
mapped default, if ``--map`` allows it.

Needs the optional extra: ``pip install "cat-fleet-chat[discord]"``, and the
Message Content intent enabled for the bot in the Discord developer portal.

Examples::

    DISCORD_BOT_TOKEN=... cat-fleet-discord --map 123456789012345678=fleet
    DISCORD_BOT_TOKEN=... cat-fleet-discord --map 123456789012345678=fleet,ops
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from cat_fleet_chat.validate import CHANNEL_RE, HANDLE_RE

DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_PREFIX = "dc-"
OK_REACTION = "✅"
FAIL_REACTION = "❌"
ATTEMPTS = 3
RETRY_DELAY = 2.0

Forward = Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]]


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


def message_text(message: Any) -> str:
    """Message body plus attachment links. ``clean_content`` resolves ``<@id>`` to names."""
    parts = [message.clean_content.strip()] if message.clean_content.strip() else []
    parts.extend(attachment.url for attachment in message.attachments)
    return "\n".join(parts)


def build_post(
    message: Any,
    hub_channel: str,
    prefix: str = DEFAULT_PREFIX,
    *,
    text: str | None = None,
    origin_channel_id: int | None = None,
) -> dict[str, Any]:
    """The hub post for a Discord message, with the ``origin`` that lets a reply find its way back."""
    return {
        "channel": hub_channel,
        "author": author_handle(message.author.display_name, prefix),
        "text": message_text(message) if text is None else text,
        # Idempotent: a retry after a timeout cannot post the message twice.
        "client_request_id": f"discord-{message.id}",
        "origin": {
            "source": "discord",
            # The mapped channel, not a thread inside it: an attached webhook posts to the channel.
            "channel_id": str(origin_channel_id if origin_channel_id is not None else message.channel.id),
            "message_id": str(message.id),
            "author": message.author.display_name[:80],
        },
    }


def make_forward(client: httpx.AsyncClient, url: str, token: str) -> Forward:
    """POST a payload to the hub. Retries network errors and 5xx; the request id makes that safe."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    endpoint = f"{url.rstrip('/')}/api/v1/messages"

    async def forward(payload: dict[str, Any]) -> tuple[bool, str]:
        reason = "unknown error"
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY * attempt)
            try:
                response = await client.post(endpoint, json=payload, headers=headers, timeout=15)
            except httpx.HTTPError as exc:
                reason = f"hub unreachable ({type(exc).__name__})"
                continue
            if response.status_code == 201:
                return True, ""
            try:
                reason = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                reason = f"HTTP {response.status_code}"
            if response.status_code < 500:
                break
        return False, reason

    return forward


ChannelList = Callable[[], Awaitable["set[str] | None"]]


def make_channels(
    client: httpx.AsyncClient, url: str, token: str, ttl: float = 30.0
) -> ChannelList:
    """The hub's open channel names, cached for ``ttl`` seconds. ``None`` when the hub was never reachable."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    endpoint = f"{url.rstrip('/')}/api/v1/channels"
    cache: dict[str, Any] = {"at": 0.0, "names": None}

    async def channels() -> set[str] | None:
        now = time.monotonic()
        if cache["names"] is not None and now - cache["at"] < ttl:
            return cache["names"]
        try:
            response = await client.get(endpoint, headers=headers, timeout=10)
            response.raise_for_status()
            names = {item["name"] for item in response.json()["channels"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return cache["names"]  # a stale list beats none
        cache.update(at=now, names=names)
        return names

    return channels


async def handle_message(
    message: Any,
    mapping: dict[int, MapEntry],
    forward: Forward,
    prefix: str = DEFAULT_PREFIX,
    channels: ChannelList | None = None,
) -> str:
    """Relay one Discord message. Returns ``ignored``, ``sent`` or ``failed``.

    Bots and webhook posts are ignored, which stops the hub's own outbound
    webhook (a Discord webhook post) from looping back in. A ``#hub-channel``
    token picks the hub channel, within what the map allows.
    """
    if message.author.bot or message.webhook_id is not None:
        return "ignored"
    channel = message.channel
    mapped_id = channel.id if channel.id in mapping else getattr(channel, "parent_id", None)  # or a thread of one
    entry = mapping.get(mapped_id)
    if entry is None:
        return "ignored"
    text = message_text(message)
    if not text:
        return "ignored"  # sticker-only or similar
    known = await channels() if channels is not None and TARGET_RE.search(text) else None
    hub_channel, text, note = parse_target(text, entry, known)
    payload = build_post(message, hub_channel, prefix, text=text, origin_channel_id=mapped_id)
    ok, reason = await forward(payload)
    try:
        if ok:
            await message.add_reaction(OK_REACTION)
            if note:
                await message.reply(f"Sent to #{hub_channel} instead: {note}", mention_author=False)
        else:
            await message.add_reaction(FAIL_REACTION)
            await message.reply(f"Not delivered to the fleet: {reason}", mention_author=False)
    except Exception as exc:  # Discord permissions are the operator's; never crash the relay
        sys.stderr.write(f"cat-fleet-discord: could not react or reply: {exc!r}\n")
    return "sent" if ok else "failed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cat-fleet-discord",
        description="Relay Discord channel messages into Cat Fleet Chat channels.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CAT_FLEET_URL", DEFAULT_URL),
        help="hub base URL (env CAT_FLEET_URL, default %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CAT_FLEET_TOKEN", ""),
        help="hub bearer token (env CAT_FLEET_TOKEN)",
    )
    parser.add_argument(
        "--discord-token",
        default=os.environ.get("DISCORD_BOT_TOKEN", ""),
        help="Discord bot token (env DISCORD_BOT_TOKEN)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="DISCORD_CHANNEL_ID=HUB_CHANNEL[,MORE|*]",
        help=(
            "relay this Discord channel into a hub channel (repeatable). The first name is the "
            "default; extra names are channels a #token may pick; * allows any"
        ),
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="prefix for relayed authors, so outbound hooks can exclude them (default %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        mapping = parse_map(args.map)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.discord_token:
        parser.error("a Discord bot token is required (--discord-token or DISCORD_BOT_TOKEN)")
    if not HANDLE_RE.fullmatch(args.prefix + "x"):
        parser.error("--prefix must be letters, digits, '_' or '-', starting with a letter or digit")
    try:
        import discord
    except ImportError:
        raise SystemExit(
            "cat-fleet-discord needs discord.py: pip install 'cat-fleet-chat[discord]'"
        ) from None

    intents = discord.Intents.default()
    intents.message_content = True

    class Relay(discord.Client):
        async def setup_hook(self) -> None:
            self.http_client = httpx.AsyncClient()
            self.forward = make_forward(self.http_client, args.url, args.token)
            self.channels = make_channels(self.http_client, args.url, args.token)

        async def on_ready(self) -> None:
            sys.stderr.write(
                f"cat-fleet-discord: logged in as {self.user}; relaying {len(mapping)} channel(s)\n"
            )

        async def on_message(self, message: Any) -> None:
            await handle_message(message, mapping, self.forward, args.prefix, self.channels)

        async def close(self) -> None:
            await super().close()
            await self.http_client.aclose()

    try:
        Relay(intents=intents).run(args.discord_token)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
