"""``cat-fleet-relay``: run a hub-configured relay, or seed one from ``--map`` flags.

Run (config comes from the hub, live)::

    CAT_FLEET_RELAY_TOKEN=rly_... DISCORD_BOT_TOKEN=... cat-fleet-relay --platform discord --name main
    CAT_FLEET_RELAY_TOKEN=rly_... SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \\
        cat-fleet-relay --platform slack --name team
    CAT_FLEET_RELAY_TOKEN=rly_... TELEGRAM_BOT_TOKEN=... cat-fleet-relay --platform telegram --name tg

Seed (one-time migration from ``cat-fleet-discord --map``; needs the fleet token)::

    CAT_FLEET_TOKEN=... cat-fleet-relay seed --platform discord --name main \\
        --map 123456789012345678=fleet,ops --operator 111 --action-user 111

``seed`` creates the relay and one binding per ``--map`` and prints the relay
token once. Give that token (not the fleet token) to the running relay.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from cat_fleet_chat.relay.core import RelayCore
from cat_fleet_chat.relay.hub_client import HubClient
from cat_fleet_chat.relay.routing import parse_map
from cat_fleet_chat.relays import NAME_RE, PLATFORMS

DEFAULT_URL = "http://127.0.0.1:8787"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cat-fleet-relay", description="Bridge a chat platform to Cat Fleet Chat.")
    parser.add_argument("mode", nargs="?", choices=("run", "seed"), default="run")
    parser.add_argument("--platform", choices=PLATFORMS, default=os.environ.get("CAT_FLEET_RELAY_PLATFORM", "discord"))
    parser.add_argument("--name", default=os.environ.get("CAT_FLEET_RELAY_NAME", ""), help="relay name in the hub")
    parser.add_argument("--url", default=os.environ.get("CAT_FLEET_URL", DEFAULT_URL), help="hub base URL")
    parser.add_argument(
        "--token",
        default=os.environ.get("CAT_FLEET_RELAY_TOKEN", ""),
        help="the relay's scoped rly_ token (env CAT_FLEET_RELAY_TOKEN)",
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("CAT_FLEET_RELAY_STATE", "."),
        help="where the config cache and Discord reaction refs are kept",
    )
    # seed only
    parser.add_argument("--map", action="append", default=[], metavar="CHANNEL_ID=HUB_CHANNEL[,MORE|*]")
    parser.add_argument("--operator", action="append", default=[], help="platform user id allowed to run write commands")
    parser.add_argument("--action-user", action="append", default=[], help="platform user id whose buttons/reactions act")
    parser.add_argument("--actor", default=os.environ.get("USER", "operator"), help="who to record in the audit log")
    return parser


async def seed(args: argparse.Namespace) -> dict[str, Any]:
    """Create the relay and its bindings with the fleet token. Returns the hub's answers."""
    admin = os.environ.get("CAT_FLEET_TOKEN", "")
    headers = {"Authorization": f"Bearer {admin}"} if admin else {}
    mapping = parse_map(args.map)
    actor = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in args.actor).strip("-_") or "operator"
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), headers=headers, timeout=15) as client:
        created = await client.post(
            "/api/v1/relays",
            json={
                "actor": actor,
                "name": args.name,
                "platform": args.platform,
                "config": {"operators": args.operator, "action_users": args.action_user},
            },
        )
        if created.status_code != 201:
            raise SystemExit(f"could not create relay {args.name}: {created.text}")
        result = created.json()
        for channel_id, entry in mapping.items():
            bound = await client.post(
                f"/api/v1/relays/{args.name}/bindings",
                json={
                    "actor": actor,
                    "external_id": str(channel_id),
                    "hub_channel": entry.default,
                    "allowed": sorted(entry.allowed - {entry.default}),
                    "any_channel": entry.any_channel,
                },
            )
            if bound.status_code != 201:
                raise SystemExit(f"could not bind {channel_id}: {bound.text}")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO, and Telegram's carries the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if not NAME_RE.fullmatch(args.name or ""):
        parser.error("--name (or CAT_FLEET_RELAY_NAME) must match [a-z0-9][a-z0-9_-]{0,39}")
    if args.mode == "seed":
        try:
            result = asyncio.run(seed(args))
        except ValueError as exc:
            parser.error(str(exc))
        sys.stdout.write(
            f"relay {args.name} created. Its token (shown once):\n{result['token']}\n"
            "Run it with CAT_FLEET_RELAY_TOKEN set to that token.\n"
        )
        return
    if not args.token.startswith("rly_"):
        parser.error("a relay token is required (--token or CAT_FLEET_RELAY_TOKEN, starts with rly_)")
    state = Path(args.state_dir)
    cache = str(state / f"cat_fleet_relay_{args.name}.json")

    def make_core(adapter: Any) -> RelayCore:
        hub = HubClient(httpx.AsyncClient(), args.url, args.token, args.name)
        return RelayCore(hub, adapter, cache_path=cache)

    if args.platform == "discord":
        from cat_fleet_chat.discord_relay import MessageRefs
        from cat_fleet_chat.relay import discord_adapter

        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            parser.error("DISCORD_BOT_TOKEN is required for --platform discord")
        refs = MessageRefs(state / f"cat_fleet_relay_{args.name}_refs.json")

        async def factory(adapter: Any) -> RelayCore:
            return make_core(adapter)

        discord_adapter.run(factory, token, refs)
    elif args.platform == "slack":
        from cat_fleet_chat.relay import slack_adapter

        slack_adapter.run(make_core, os.environ.get("SLACK_BOT_TOKEN", ""), os.environ.get("SLACK_APP_TOKEN", ""))
    else:
        from cat_fleet_chat.relay import telegram_adapter

        telegram_adapter.run(make_core, os.environ.get("TELEGRAM_BOT_TOKEN", ""))


if __name__ == "__main__":
    main()
