"""``cat-fleet-discord``: relay Discord channel messages into Cat Fleet Chat.

Discord cannot call a webhook when someone types, so inbound from Discord needs
a bot. This process is that bot and a plain client of the hub, like
``cat-fleet-listen``: it logs in to Discord, and POSTs each message from a
mapped channel to ``/api/v1/messages``. It needs no public URL.

The other direction (hub to Discord) is an outbound webhook of format
``discord``; give it ``exclude_authors=["dc-*"]`` so relayed messages are not
echoed back to the channel they came from.

Needs the optional extra: ``pip install "cat-fleet-chat[discord]"``, and the
Message Content intent enabled for the bot in the Discord developer portal.

Example::

    DISCORD_BOT_TOKEN=... cat-fleet-discord --map 123456789012345678=fleet
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
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


def parse_map(items: list[str]) -> dict[int, str]:
    """``["123=fleet", "456=ops"]`` into ``{123: "fleet", 456: "ops"}``. Rejects malformed entries."""
    mapping: dict[int, str] = {}
    for item in items:
        left, sep, right = item.partition("=")
        if not sep or not left.strip().isdigit() or not CHANNEL_RE.fullmatch(right.strip()):
            raise ValueError(f"--map expects <discord_channel_id>=<hub_channel>, got {item!r}")
        mapping[int(left.strip())] = right.strip()
    if not mapping:
        raise ValueError("at least one --map is required")
    return mapping


def author_handle(display_name: str, prefix: str = DEFAULT_PREFIX) -> str:
    """A hub handle for a Discord user. Always starts with ``prefix`` so hooks can filter on it."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_")
    return (prefix + (slug or "user"))[:64].rstrip("-_")


def message_text(message: Any) -> str:
    """Message body plus attachment links. ``clean_content`` resolves ``<@id>`` to names."""
    parts = [message.clean_content.strip()] if message.clean_content.strip() else []
    parts.extend(attachment.url for attachment in message.attachments)
    return "\n".join(parts)


def build_post(message: Any, hub_channel: str, prefix: str = DEFAULT_PREFIX) -> dict[str, Any]:
    return {
        "channel": hub_channel,
        "author": author_handle(message.author.display_name, prefix),
        "text": message_text(message),
        # Idempotent: a retry after a timeout cannot post the message twice.
        "client_request_id": f"discord-{message.id}",
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


async def handle_message(
    message: Any,
    mapping: dict[int, str],
    forward: Forward,
    prefix: str = DEFAULT_PREFIX,
) -> str:
    """Relay one Discord message. Returns ``ignored``, ``sent`` or ``failed``.

    Bots and webhook posts are ignored, which stops the hub's own outbound
    webhook (a Discord webhook post) from looping back in.
    """
    if message.author.bot or message.webhook_id is not None:
        return "ignored"
    channel = message.channel
    hub_channel = mapping.get(channel.id)
    if hub_channel is None:
        hub_channel = mapping.get(getattr(channel, "parent_id", None))  # a thread of a mapped channel
    if hub_channel is None:
        return "ignored"
    payload = build_post(message, hub_channel, prefix)
    if not payload["text"]:
        return "ignored"  # sticker-only or similar
    ok, reason = await forward(payload)
    try:
        if ok:
            await message.add_reaction(OK_REACTION)
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
        metavar="DISCORD_CHANNEL_ID=HUB_CHANNEL",
        help="relay this Discord channel into this hub channel (repeatable)",
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

        async def on_ready(self) -> None:
            sys.stderr.write(
                f"cat-fleet-discord: logged in as {self.user}; relaying {len(mapping)} channel(s)\n"
            )

        async def on_message(self, message: Any) -> None:
            await handle_message(message, mapping, self.forward, args.prefix)

        async def close(self) -> None:
            await super().close()
            await self.http_client.aclose()

    try:
        Relay(intents=intents).run(args.discord_token)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
