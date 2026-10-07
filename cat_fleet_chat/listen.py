"""``cat-fleet-listen``: deliver hub events to an external CLI agent.

Long-polls ``GET /api/v1/notifications`` and emits every relevant event as a
JSON line on stdout, or runs ``--exec`` once per event with the event JSON on
stdin. This process pulls; to have the hub push to a URL instead, use an
outbound webhook (see webhooks.py).

Examples::

    cat-fleet-listen --agent codex                  # stream JSON lines
    cat-fleet-listen --agent claude --once          # block until one batch, print, exit
    cat-fleet-listen --agent grok --exec "python on_event.py" --state-file .fleet-cursor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8787"
POLL_TIMEOUT = 60
BACKOFF_MAX = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cat-fleet-listen",
        description="Pull Cat Fleet Chat events relevant to one agent.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CAT_FLEET_URL", DEFAULT_URL),
        help="hub base URL (env CAT_FLEET_URL, default %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CAT_FLEET_TOKEN", ""),
        help="bearer token (env CAT_FLEET_TOKEN)",
    )
    parser.add_argument("--agent", help="handle to notify; omit for every event")
    parser.add_argument("--kinds", help="comma-separated event kinds, e.g. message.created")
    parser.add_argument("--channel", help="only events from this channel")
    parser.add_argument(
        "--after",
        default="now",
        help="event id to start after, or 'now' to skip history (default)",
    )
    parser.add_argument("--state-file", help="persist the cursor here between runs")
    parser.add_argument("--once", action="store_true", help="exit after the first batch")
    parser.add_argument("--exec", dest="exec_cmd", help="shell command to run per event")
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=POLL_TIMEOUT,
        help="seconds the hub holds each poll, 1..300 (default %(default)s)",
    )
    return parser


def _read_state(path: str | None) -> int | None:
    if not path:
        return None
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_state(path: str | None, cursor: int) -> None:
    if not path:
        return
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(str(cursor), encoding="utf-8")
    tmp.replace(target)


def _headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def run_exec(command: str, event: dict[str, Any]) -> int:
    """Run ``command`` with the event on stdin and a few fields in the environment."""
    env = {
        **os.environ,
        "CAT_FLEET_EVENT_ID": str(event.get("id", "")),
        "CAT_FLEET_EVENT_KIND": str(event.get("kind", "")),
        "CAT_FLEET_EVENT_CHANNEL": str(event.get("channel") or ""),
    }
    # The operator supplies this command on their own command line.
    completed = subprocess.run(
        command,
        shell=True,
        input=json.dumps(event),
        text=True,
        env=env,
        check=False,
    )
    return completed.returncode


async def _start_cursor(client: httpx.AsyncClient, args: argparse.Namespace) -> int:
    saved = _read_state(args.state_file)
    if saved is not None:
        return saved
    if str(args.after).lower() != "now":
        try:
            return max(0, int(args.after))
        except ValueError:
            # The retry loop would catch a ValueError and spin forever; exit instead.
            sys.stderr.write("cat-fleet-listen: --after must be an event id or 'now'\n")
            raise SystemExit(2) from None
    response = await client.get(
        f"{args.url.rstrip('/')}/api/v1/events/cursor", headers=_headers(args.token)
    )
    response.raise_for_status()
    return int(response.json()["event_cursor"])


async def listen(
    args: argparse.Namespace,
    client: httpx.AsyncClient,
    *,
    emit: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> int:
    """Poll until interrupted, or until one non-empty batch with ``--once``.

    Returns the last cursor. A failed poll backs off (1s doubling to 30s)
    and keeps the cursor, so no event is skipped.
    """

    def default_emit(event: dict[str, Any]) -> None:
        if args.exec_cmd:
            run_exec(args.exec_cmd, event)
        else:
            sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stdout.flush()

    deliver = emit or default_emit
    base = args.url.rstrip("/")
    backoff = 1.0
    cursor: int | None = None
    while True:
        try:
            if cursor is None:
                cursor = await _start_cursor(client, args)
            params: dict[str, Any] = {
                "after_event_id": cursor,
                "timeout": args.poll_timeout,
            }
            for name in ("agent", "kinds", "channel"):
                value = getattr(args, name)
                if value:
                    params[name] = value
            response = await client.get(
                f"{base}/api/v1/notifications",
                params=params,
                headers=_headers(args.token),
                timeout=httpx.Timeout(args.poll_timeout + 15),
            )
            if 400 <= response.status_code < 500:
                # Bad agent name, unknown channel, wrong token: retrying will not help.
                sys.stderr.write(f"cat-fleet-listen: {response.status_code} {response.text}\n")
                raise SystemExit(2)
            response.raise_for_status()
            page = response.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            sys.stderr.write(f"cat-fleet-listen: {exc!r}; retrying in {backoff:.0f}s\n")
            await sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        backoff = 1.0
        for event in page["events"]:
            deliver(event)
        cursor = int(page["cursor"])
        _write_state(args.state_file, cursor)
        if args.once and page["events"]:
            return cursor


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    async def runner() -> None:
        async with httpx.AsyncClient() as client:
            await listen(args, client)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
