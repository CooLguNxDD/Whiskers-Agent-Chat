"""Outbound webhooks: the only place the hub makes outbound HTTP calls.

``WebhookDispatcher`` follows the durable ``events`` log, the same way the SSE
route does, and POSTs matching events to each enabled outbound hook. Every hook
keeps its own cursor in the database, so a restart resumes where it stopped.

Delivery is at-least-once: a hook's cursor advances only after a 2xx response.
A failed batch is retried with backoff; a hook that the receiver rejects for
good (401/403/404/410, e.g. a deleted Discord webhook) is disabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

import httpx

from cat_fleet_chat.hub import Hub, agent_relevant

logger = logging.getLogger("cat_fleet_chat.webhooks")

BATCH = 50
IDLE_WAKE = 15.0
BACKOFF = (5.0, 30.0, 120.0, 300.0)
RETRY_AFTER_MAX = 300.0
CHAT_LIMIT = 2_000
MESSAGE_CLIP = 600
ERROR_CLIP = 300
USER_AGENT = "cat-fleet-chat-webhook/1"


# ---- matching ----------------------------------------------------------


def event_actor(event: dict[str, Any]) -> str | None:
    """Who caused ``event``: message author, last task actor, or channel actor."""
    payload = event.get("payload") or {}
    kind = event["kind"]
    if kind == "message.created":
        return payload.get("author")
    if kind == "task.updated":
        history = payload.get("events") or []
        return history[-1].get("actor") if history else None
    return payload.get("actor")


def event_destination(event: dict[str, Any]) -> str | None:
    """The outbound hook a message is addressed to, if any."""
    if event["kind"] != "message.created":
        return None
    return (event.get("payload") or {}).get("destination") or None


def matches(hook: dict[str, Any], event: dict[str, Any]) -> bool:
    """Whether ``event`` should be sent to ``hook``.

    A message addressed to this hook always matches, whatever its filters say.
    A ``directed_only`` hook matches nothing else. Otherwise an empty filter
    lets everything through.
    """
    destination = event_destination(event)
    if hook.get("directed_only"):
        return destination == hook["name"]
    if destination is not None and destination == hook["name"]:
        return True
    if hook["kinds"] and event["kind"] not in hook["kinds"]:
        return False
    if hook["channels"] and event.get("channel") not in hook["channels"]:
        return False
    if hook["mentions"] and not any(
        # Channel lifecycle events are broadcast to every agent; a "mentions"
        # filter is about messages and assignments only.
        agent_relevant(event, name) and not event["kind"].startswith("channel.")
        for name in hook["mentions"]
    ):
        return False
    actor = event_actor(event)
    if actor and any(fnmatchcase(actor, pattern) for pattern in hook["exclude_authors"]):
        return False
    return True


# ---- formatting --------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


QUOTE_CLIP = 120


def event_line(event: dict[str, Any], parents: dict[int, dict[str, Any]] | None = None) -> str:
    """One human-readable entry for ``event``. ``**x**`` marks bold.

    ``parents`` holds the messages that replies point at. An addressed reply
    is shown as a quote of the request followed by the answer, since a
    webhook cannot post a native Discord reply.
    """
    kind = event["kind"]
    payload = event.get("payload") or {}
    channel = event.get("channel")
    where = f"**#{channel}** " if channel else ""
    if kind == "message.created":
        text = _clip(str(payload.get("text", "")), MESSAGE_CLIP)
        line = f"{where}`{payload.get('author', '?')}`: {text}"
        parent = (parents or {}).get(payload.get("reply_to")) if payload.get("destination") else None
        if parent:
            snippet = _clip(" ".join(str(parent.get("text", "")).split()), QUOTE_CLIP)
            return f"> `{parent.get('author', '?')}`: {snippet}\n{line}"
        return line
    if kind == "task.updated":
        history = payload.get("events") or []
        last = history[-1] if history else {}
        actor = last.get("actor")
        target = payload.get("status")
        previous = last.get("from_status")
        change = f"created as {target}" if previous is None else f"{previous} → {target}"
        line = f"📋 {where}task #{payload.get('id')} \"{_clip(str(payload.get('title', '')), 120)}\": {change}"
        if payload.get("assignee"):
            line += f" (assignee `{payload['assignee']}`)"
        if actor:
            line += f" by `{actor}`"
        note = last.get("note")
        if note:
            line += f" — {_clip(str(note), 200)}"
        return line
    if kind == "channel.created":
        return f"📁 channel **#{payload.get('name', channel)}** created"
    if kind in {"channel.archived", "channel.unarchived"}:
        verb = kind.split(".", 1)[1]
        by = f" by `{payload['actor']}`" if payload.get("actor") else ""
        return f"🗄️ channel **#{payload.get('name', channel)}** {verb}{by}"
    if kind == "channel.state_changed":
        by = f" by `{payload['actor']}`" if payload.get("actor") else ""
        note = f" — {_clip(str(payload['note']), 200)}" if payload.get("note") else ""
        return (
            f"🚦 channel **#{payload.get('name', channel)}**: "
            f"{payload.get('previous_state')} → {payload.get('state')}{by}{note}"
        )
    if kind == "webhook.ping":
        return f"🔔 Cat Fleet webhook **{payload.get('name')}** is connected"
    return f"{kind} #{event.get('id')}"


def chat_payloads(
    events: list[dict[str, Any]],
    fmt: str,
    parents: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Discord or Slack bodies for ``events``. Lines are coalesced up to the 2000-char limit."""
    lines = [event_line(event, parents) for event in events]
    if fmt == "slack":
        # Slack bold is *x*; defang <!channel>, <!here> and <@user> so message
        # text from an agent cannot ping people.
        lines = [
            line.replace("**", "*").replace("<!", "<​!").replace("<@", "<​@")
            for line in lines
        ]
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = _clip(line, CHAT_LIMIT)
        if current and len(current) + 1 + len(line) > CHAT_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    if fmt == "discord":
        # parse=[] turns every @everyone/@here/<@id> in agent text into plain text.
        return [
            {"content": chunk, "username": "Cat Fleet", "allowed_mentions": {"parse": []}}
            for chunk in chunks
        ]
    return [{"text": chunk} for chunk in chunks]


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def generic_request(hook: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    return {
        "content": body,
        "headers": {
            "Content-Type": "application/json",
            "X-CatFleet-Event": event["kind"],
            "X-CatFleet-Delivery": str(event["id"]),
            "X-CatFleet-Signature": sign(hook["secret"], body),
        },
    }


# ---- delivery ----------------------------------------------------------


@dataclass
class _Request:
    kwargs: dict[str, Any]
    # Event id the cursor may advance to once this request succeeds.
    upto: int | None = None


@dataclass
class Outcome:
    kind: str  # ok | retry | skip | disable
    status: int | None = None
    error: str | None = None
    delay: float = 0.0
    label: str = field(default="")

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def _redact(text: str, hook: dict[str, Any]) -> str:
    # The URL of a Discord/Slack hook carries its token; never store it in last_error.
    return _clip(text.replace(hook["url"], "<url>"), ERROR_CLIP)


def classify(response: httpx.Response, hook: dict[str, Any]) -> Outcome:
    code = response.status_code
    if 200 <= code < 300:
        return Outcome("ok", code, label="ok")
    snippet = _redact(response.text.strip().replace("\n", " "), hook)
    error = f"HTTP {code}" + (f": {snippet}" if snippet else "")
    if code == 429:
        try:
            delay = float(response.headers.get("retry-after", ""))
        except ValueError:
            delay = BACKOFF[0]
        return Outcome("retry", code, error, min(max(delay, 1.0), RETRY_AFTER_MAX), "429")
    if code in {401, 403, 404, 410}:
        return Outcome("disable", code, error, label=str(code))
    if code >= 500:
        return Outcome("retry", code, error, label=str(code))
    # Other 4xx and 3xx: the receiver will not accept this payload. Skip it.
    return Outcome("skip", code, error, label=str(code))


class WebhookDispatcher:
    def __init__(
        self,
        hub: Hub,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = 2.0,
    ) -> None:
        self.hub = hub
        self.min_interval = min_interval
        self._client = httpx.AsyncClient(
            timeout=10.0, transport=transport, headers={"User-Agent": USER_AGENT}
        )
        self._task: asyncio.Task | None = None
        self._link_task: asyncio.Task | None = None
        self._retry_at: dict[int, float] = {}
        self._last_sent: dict[int, float] = {}

    # lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="webhook-dispatcher")
            # Separate task: a slow lookup must not hold up deliveries.
            self._link_task = asyncio.create_task(self._link_at_startup(), name="webhook-link")

    async def stop(self) -> None:
        for task in (self._task, self._link_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = self._link_task = None
        await self._client.aclose()

    # loop

    async def _run(self) -> None:
        while True:
            try:
                wait = await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("webhook dispatcher pass failed")
                wait = IDLE_WAKE
            condition = self.hub.waiters.condition
            async with condition:
                try:
                    # Checked under the lock so a commit between pump() and wait() is not lost.
                    if await self._pending():
                        continue
                    await asyncio.wait_for(condition.wait(), min(IDLE_WAKE, max(wait, 0.05)))
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("webhook dispatcher wait failed")
                    await asyncio.sleep(1.0)

    async def _pending(self) -> bool:
        latest = int((await self.hub.event_cursor())["event_cursor"])
        now = time.monotonic()
        return any(
            hook["cursor"] < latest and self._retry_at.get(hook["id"], 0.0) <= now
            for hook in await self.hub.outbound_webhooks()
        )

    async def pump(self) -> float:
        """Deliver to every due hook once. Returns seconds until the next retry is due."""
        hooks = await self.hub.outbound_webhooks()
        live = {hook["id"] for hook in hooks}
        for table in (self._retry_at, self._last_sent):
            for stale in set(table) - live:
                del table[stale]
        now = time.monotonic()
        due = [hook for hook in hooks if self._retry_at.get(hook["id"], 0.0) <= now]
        results = await asyncio.gather(*(self._deliver(hook) for hook in due), return_exceptions=True)
        for hook, result in zip(due, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                logger.error("webhook %s delivery crashed: %r", hook["name"], result)
                self._retry_at[hook["id"]] = time.monotonic() + BACKOFF[0]
        now = time.monotonic()
        upcoming = [at - now for at in self._retry_at.values() if at > now]
        return min(upcoming) if upcoming else IDLE_WAKE

    # one hook

    async def _parents(self, chosen: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """The messages that addressed replies point at, so they can be quoted."""
        ids = {
            int(payload["reply_to"])
            for event in chosen
            if event["kind"] == "message.created"
            and (payload := event.get("payload") or {}).get("reply_to")
            and payload.get("destination")
        }
        return await self.hub.message_context(sorted(ids)) if ids else {}

    def _requests(
        self,
        hook: dict[str, Any],
        chosen: list[dict[str, Any]],
        parents: dict[int, dict[str, Any]] | None = None,
    ) -> list[_Request]:
        if hook["format"] == "generic":
            return [_Request(generic_request(hook, event), int(event["id"])) for event in chosen]
        payloads = chat_payloads(chosen, hook["format"], parents)
        last = int(chosen[-1]["id"])
        # Only the final chunk moves the cursor to the end of the batch.
        return [
            _Request({"json": body}, last if index == len(payloads) - 1 else None)
            for index, body in enumerate(payloads)
        ]

    async def _deliver(self, hook: dict[str, Any]) -> None:
        cursor = int(hook["cursor"])
        failures = int(hook["failure_count"])
        status: str | None = None
        error: str | None = None
        while True:
            batch = await self.hub.events_after(cursor, channel=None, limit=BATCH)
            if not batch:
                break
            reached = int(batch[-1]["id"])
            chosen = [event for event in batch if matches(hook, event)]
            requests = self._requests(hook, chosen, await self._parents(chosen)) if chosen else []
            for request in requests:
                outcome = await self._post(hook, request.kwargs)
                if outcome.kind in {"retry", "disable"}:
                    await self._fail(hook, outcome, cursor, failures)
                    return
                if request.upto is not None:
                    cursor = max(cursor, request.upto)
                status = outcome.label
                error = outcome.error if outcome.kind == "skip" else None
                if outcome.kind == "skip":
                    logger.warning("webhook %s skipped a payload: %s", hook["name"], outcome.error)
            cursor = reached
            # Events that matched nothing still advance the cursor, quietly.
            await self.hub.record_delivery(
                hook["id"],
                status=status,
                error=error,
                cursor=cursor,
                failure_count=0,
            )
            failures = 0
            self._retry_at.pop(hook["id"], None)
            if len(batch) < BATCH:
                break

    async def _fail(
        self, hook: dict[str, Any], outcome: Outcome, cursor: int, failures: int
    ) -> None:
        disable = outcome.kind == "disable"
        count = failures + 1
        await self.hub.record_delivery(
            hook["id"],
            status=("disabled:" if disable else "retry:") + outcome.label,
            error=outcome.error,
            cursor=cursor,
            failure_count=count,
            disable=disable,
        )
        if disable:
            logger.warning("webhook %s disabled: %s", hook["name"], outcome.error)
            self._retry_at.pop(hook["id"], None)
            return
        delay = outcome.delay or BACKOFF[min(count - 1, len(BACKOFF) - 1)]
        self._retry_at[hook["id"]] = time.monotonic() + delay

    async def _post(self, hook: dict[str, Any], kwargs: dict[str, Any]) -> Outcome:
        if hook["format"] != "generic" and self.min_interval > 0:
            wait = self._last_sent.get(hook["id"], 0.0) + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        try:
            response = await self._client.post(hook["url"], **kwargs)
        except httpx.HTTPError as exc:
            return Outcome(
                "retry", None, _redact(f"{type(exc).__name__}: {exc}", hook), label="error"
            )
        finally:
            self._last_sent[hook["id"]] = time.monotonic()
        return classify(response, hook)

    # Discord lookup

    async def discord_info(self, url: str) -> dict[str, Any] | None:
        """Which channel a Discord webhook URL posts to (``channel_id``). ``None`` on any failure.

        Discord answers a plain GET on the webhook URL, no auth needed. Kept
        here so every outbound HTTP call in the hub lives in this class.
        """
        try:
            response = await self._client.get(url, timeout=5.0)
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("channel_id") else None

    async def link_discord_channels(self) -> None:
        """Fill in ``discord_channel_id`` for Discord hooks that have none.

        Hooks created before replies could be routed have no link, and a lookup
        that failed at creation leaves none. Runs once at startup. A hook that
        still cannot be resolved is left alone; set its id by hand.
        """
        for hook in await self.hub.outbound_webhooks():
            if hook["format"] != "discord" or hook.get("discord_channel_id"):
                continue
            info = await self.discord_info(hook["url"])
            if info:
                await self.hub.update_webhook(
                    hook["id"], {"discord_channel_id": str(info["channel_id"])}
                )

    async def _link_at_startup(self) -> None:
        try:
            await self.link_discord_channels()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("could not link Discord channels at startup", exc_info=True)

    # test ping

    async def send_test(self, hook: dict[str, Any]) -> dict[str, Any]:
        """Send a synthetic ``webhook.ping`` now. Touches neither the event log nor the cursor."""
        event = {
            "id": 0,
            "kind": "webhook.ping",
            "channel_id": None,
            "channel": None,
            "entity_id": 0,
            "payload": {"name": hook["name"]},
            "created_at": None,
        }
        if hook["format"] == "generic":
            kwargs = generic_request(hook, event)
        else:
            kwargs = {"json": chat_payloads([event], hook["format"])[0]}
        outcome = await self._post(hook, kwargs)
        return {"ok": outcome.ok, "status": outcome.status, "error": outcome.error}
