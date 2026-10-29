"""``RelayCore``: everything a relay does that is not platform-specific.

- **Inbound**: a human message in a bound channel or thread goes to its hub
  channel (``#channel`` targeting within the binding's allowlist), masked,
  under a ``<prefix><name>`` handle, with an ``origin`` naming this relay so
  answers come back through it.
- **Replies**: messages addressed to ``relay:<name>`` arrive on the relay's
  replies consumer and are posted where the conversation came from.
- **Routes**: each enabled route's consumer is delivered to its platform
  channel as a line, an embed or a card.
- **Commands and buttons**: ``/fleet …`` and card buttons, gated by the
  relay's operators and action users; the hub still checks every action.
- **Heartbeat**: every ``heartbeat_every`` seconds, with a small status; a
  new config version reloads bindings and routes and reconciles the loops.

Delivery is at-least-once: a consumer is acked only after the platform
accepted the message. An event that fails ``POISON_AFTER`` times is skipped
(acked) and counted, so one undeliverable message cannot stall a route.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from typing import Any

from cat_fleet_chat import mask as masking
from cat_fleet_chat.relay import commands
from cat_fleet_chat.relay.adapter import Adapter, ButtonPress, CommandCall, Inbound, Reply
from cat_fleet_chat.relay.config import ConfigCache, RelayConfig
from cat_fleet_chat.relay.hub_client import HubClient
from cat_fleet_chat.relay.render import Rendered, parse_button, render, render_reply
from cat_fleet_chat.relay.routing import TARGET_RE, author_handle, parse_target

logger = logging.getLogger("cat_fleet_chat.relay")

HEARTBEAT_EVERY = 15.0
WAIT_TIMEOUT = 30
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
POISON_AFTER = 5
KEY_MAX = 120


@dataclass(frozen=True)
class InboundResult:
    status: str  # ignored | sent | failed
    note: str | None = None
    reason: str | None = None
    hub_message_id: int | None = None
    hub_channel: str | None = None


class RelayCore:
    def __init__(
        self,
        hub: HubClient,
        adapter: Adapter,
        *,
        cache_path: str | None = None,
        heartbeat_every: float = HEARTBEAT_EVERY,
        wait_timeout: int = WAIT_TIMEOUT,
    ) -> None:
        self.hub = hub
        self.adapter = adapter
        self.name = hub.relay
        self.cache = ConfigCache(cache_path)
        self.heartbeat_every = heartbeat_every
        self.wait_timeout = wait_timeout
        self.config: RelayConfig | None = None
        self.stats = {"delivered": 0, "failed": 0, "skipped": 0, "inbound": 0}
        self.last_error: str | None = None
        self._loops: dict[str, asyncio.Task] = {}
        self._failures: dict[tuple[str, int], int] = {}
        self._heartbeat: asyncio.Task | None = None
        self._reload_lock = asyncio.Lock()
        # Delivery loops run only between start() and stop(); before that a
        # reload just refreshes config (commands, tests, a dry run).
        self._running = False

    # ---- config ------------------------------------------------------------------

    async def load(self) -> bool:
        """Fetch config from the hub, falling back to the cache. ``False`` when neither exists."""
        ok, snapshot = await self.hub.config()
        if ok:
            self.cache.save(snapshot)
        else:
            logger.warning("relay %s: hub config unavailable (%s); using cache", self.name, snapshot)
            snapshot = self.cache.load()
            if snapshot is None:
                return False
        self.config = RelayConfig.from_snapshot(snapshot)
        return True

    async def reload(self) -> None:
        """Reload config now and reconcile the delivery loops."""
        async with self._reload_lock:
            if await self.load() and self._running:
                self._reconcile()

    def handle(self, user_name: str) -> str:
        return author_handle(user_name, self.config.prefix if self.config else "dc-")

    # ---- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        if not await self.load():
            raise RuntimeError(f"relay {self.name}: no config from the hub and no cache")
        self._running = True
        self._reconcile()
        self._heartbeat = asyncio.create_task(self._heartbeat_loop(), name=f"relay-{self.name}-heartbeat")

    async def stop(self) -> None:
        self._running = False
        tasks = [*self._loops.values(), *([self._heartbeat] if self._heartbeat else [])]
        self._loops.clear()
        self._heartbeat = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _wanted(self) -> dict[str, tuple[str, dict[str, Any] | None]]:
        """Loop key -> (consumer name, route or None for replies)."""
        config = self.config
        if config is None or not config.enabled:
            return {}
        wanted: dict[str, tuple[str, dict[str, Any] | None]] = {}
        if config.replies_consumer:
            wanted["replies"] = (config.replies_consumer, None)
        for route in config.active_routes():
            key = f"route-{route['id']}-{route['external_id']}-{route['format']}"
            wanted[key] = (route["consumer"]["name"], route)
        return wanted

    def _reconcile(self) -> None:
        wanted = self._wanted()
        for key in list(self._loops):
            if key not in wanted:
                self._loops.pop(key).cancel()
        for key, (consumer, route) in wanted.items():
            task = self._loops.get(key)
            if task is None or task.done():
                self._loops[key] = asyncio.create_task(
                    self._deliver_loop(consumer, route), name=f"relay-{self.name}-{key}"
                )

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "platform": self.adapter.platform,
            "config_version": self.config.version if self.config else None,
            "loops": len(self._loops),
            **self.stats,
        }
        if self.last_error:
            out["last_error"] = self.last_error[:200]
        try:
            out["adapter"] = self.adapter.status()
        except Exception:  # noqa: BLE001 - status is best effort
            logger.debug("relay %s: adapter status failed", self.name, exc_info=True)
        return out

    async def _heartbeat_loop(self) -> None:
        while True:
            ok, body = await self.hub.heartbeat(self.status())
            if ok and self.config is not None and (
                body.get("version") != self.config.version or body.get("enabled") != self.config.enabled
            ):
                await self.reload()
            elif not ok:
                self.last_error = f"heartbeat: {body}"
            await asyncio.sleep(self.heartbeat_every)

    # ---- delivery -----------------------------------------------------------------------

    async def _deliver_loop(self, consumer: str, route: dict[str, Any] | None) -> None:
        delay = BACKOFF_START
        while True:
            try:
                progressed = await self.deliver_once(consumer, route, timeout=self.wait_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a loop must survive anything
                logger.exception("relay %s: delivery loop %s crashed", self.name, consumer)
                self.last_error = f"{consumer}: {type(exc).__name__}"
                progressed = False
            if progressed is None:
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
            else:
                delay = BACKOFF_START

    async def deliver_once(
        self, consumer: str, route: dict[str, Any] | None, *, timeout: int = 0
    ) -> bool | None:
        """One wait, send and ack round. ``None`` means back off (hub or platform failure)."""
        ok, body = await self.hub.consumer_wait(consumer, timeout)
        if not ok:
            self.last_error = f"{consumer}: {body}"
            return None
        events = body.get("events") or []
        if not events:
            return False
        for event in events:
            outcome = await self._deliver_event(event, route)
            key = (consumer, int(event["id"]))
            if outcome is False:
                count = self._failures.get(key, 0) + 1
                self._failures[key] = count
                if count < POISON_AFTER:
                    return None  # not acked: the same event comes again after the backoff
                logger.error("relay %s: giving up on event %s for %s", self.name, event["id"], consumer)
                self.stats["skipped"] += 1
            self._failures.pop(key, None)
            await self.hub.consumer_ack(consumer, int(event["id"]))
        return True

    async def _deliver_event(self, event: dict[str, Any], route: dict[str, Any] | None) -> bool:
        """Send one event. ``True`` when sent or deliberately dropped, ``False`` to retry."""
        if route is None:
            message = event.get("payload") or {}
            ok, body = await self.hub.reply_target(int(message.get("id", event.get("entity_id", 0))))
            if not ok:
                self.last_error = f"reply target: {body}"
                return False
            target = body.get("target")
            if target is None:
                logger.warning("relay %s: nowhere to post message %s", self.name, message.get("id"))
                return True
            external = target["external_id"]
            rendered = render_reply(message, reply_to_external=target.get("reply_to_external"))
        else:
            external = route["external_id"]
            if self._echo(event, external):
                return True
            rendered = render(event, route["format"])
        rendered = self._masked(rendered)
        nonce = f"cf{route['id'] if route else 0}-{event['id']}"[:25]
        try:
            await self.adapter.send(external, rendered, nonce=nonce)
        except Exception as exc:  # noqa: BLE001 - the platform's errors are many
            self.stats["failed"] += 1
            self.last_error = f"send to {external}: {type(exc).__name__}: {exc}"[:300]
            logger.warning("relay %s: %s", self.name, self.last_error)
            return False
        self.stats["delivered"] += 1
        return True

    def _echo(self, event: dict[str, Any], external: str) -> bool:
        """A message that came in from this very platform channel is not posted back to it."""
        if event["kind"] != "message.created":
            return False
        origin = (event.get("payload") or {}).get("origin") or {}
        return origin.get("relay") == self.name and external in (origin.get("thread_id"), origin.get("channel_id"))

    def _masked(self, rendered: Rendered) -> Rendered:
        if not (self.config and self.config.settings.get("mask", True)):
            return rendered
        text, _ = masking.mask(rendered.text)
        body = masking.mask(rendered.body)[0] if rendered.body else rendered.body
        return replace(rendered, text=text, body=body)

    # ---- inbound ---------------------------------------------------------------------------

    def _origin(self, channel_id: str, parent_id: str | None, message_id: str, user_id: str, user_name: str) -> dict[str, Any]:
        origin: dict[str, Any] = {
            "source": self.adapter.platform,
            "channel_id": parent_id or channel_id,
            "message_id": message_id,
            "user_id": user_id,
            "author": user_name[:80],
            "relay": self.name,
        }
        if parent_id is not None:
            origin["thread_id"] = channel_id
        return origin

    def _key(self, kind: str, *parts: str) -> str:
        return "-".join([self.adapter.platform, self.name, kind, *parts])[:KEY_MAX]

    async def inbound(self, message: Inbound) -> InboundResult:
        """Relay one platform message into the hub."""
        config = self.config
        if config is None or not config.enabled or message.is_bot:
            return InboundResult("ignored")
        binding = config.binding_for(message.channel_id, message.parent_id)
        if binding is None or binding.get("direction") == "out":
            return InboundResult("ignored")
        text = "\n".join([part for part in [message.text.strip(), *message.attachments] if part])
        if not text:
            return InboundResult("ignored")
        note = None
        if config.settings.get("mask", True):
            text, found = masking.mask(text)
            if found:
                note = "A credential-looking value was masked: " + ", ".join(found)
        known = await self.hub.channels() if TARGET_RE.search(text) else None
        hub_channel, text, target_note = parse_target(text, config.entry(binding), known)
        note = "; ".join(item for item in (note, target_note and f"Sent to #{hub_channel} instead: {target_note}") if item) or None
        payload = {
            "channel": hub_channel,
            "author": self.handle(message.user_name),
            "text": text,
            "client_request_id": self._key("msg", message.message_id),
            "origin": self._origin(message.channel_id, message.parent_id, message.message_id, message.user_id, message.user_name),
        }
        ok, body = await self.hub.post_message(payload)
        if not ok:
            return InboundResult("failed", reason=str(body))
        self.stats["inbound"] += 1
        return InboundResult(
            "sent", note=note, hub_message_id=body["message"]["id"], hub_channel=hub_channel
        )

    async def post_as(self, call: CommandCall, channel: str, text: str, *, key: str) -> tuple[bool, Any]:
        """Post into a hub channel as the human behind a command or button."""
        if self.config and self.config.settings.get("mask", True):
            text = masking.mask(text)[0]
        payload = {
            "channel": channel,
            "author": self.handle(call.user_name),
            "text": text,
            "client_request_id": self._key(key),
            "origin": self._origin(call.channel_id, call.parent_id, "", call.user_id, call.user_name),
        }
        payload["origin"].pop("message_id")  # a command or click has no platform message of its own
        ok, body = await self.hub.post_message(payload)
        return ok, (body if not ok else body.get("message"))

    # ---- commands and buttons --------------------------------------------------------------

    async def command(self, call: CommandCall) -> Reply:
        if self.config is None or not self.config.enabled:
            return Reply("The relay is disabled.", ok=False)
        try:
            return await commands.dispatch(self, call)
        except Exception:  # noqa: BLE001 - never crash the platform event handler
            logger.exception("relay %s: command %s failed", self.name, call.name)
            return Reply("That command failed inside the relay.", ok=False)

    async def button(self, press: ButtonPress) -> Reply:
        """Act on a card button. Needs an action user; the hub checks the action itself."""
        config = self.config
        action = parse_button(press.custom_id)
        if config is None or action is None:
            return Reply("This button is not recognised.", ok=False)
        if not config.is_action_user(press.user_id):
            return Reply("You are not allowed to act on fleet cards.", ok=False)
        handle = self.handle(press.user_name)
        call = CommandCall("button", {}, press.channel_id, press.user_id, press.user_name, press.parent_id)
        if action.kind == "proposal":
            binding = config.binding_for(press.channel_id, press.parent_id)
            if binding is None:
                return Reply("This channel is not bound to the fleet.", ok=False)
            if action.verb == "reject" and not (press.reason or "").strip():
                return Reply("A reason is required to reject.", ok=False)
            text = f"approve {action.ref}" if action.verb == "approve" else f"reject {action.ref} {press.reason.strip()}"
            ok, body = await self.post_as(
                call, binding["hub_channel"], text, key=f"btn-{action.verb}-{action.ref}-{press.user_id}"
            )
            return Reply(f"Sent `{text}` to #{binding['hub_channel']}." if ok else f"Not delivered: {body}", ok=ok)
        if action.kind == "task":
            task_id, version = action.task
            if action.verb == "claim":
                ok, body = await self.hub.claim_task(task_id, handle, version)
            else:
                status = "in_progress" if action.verb == "start" else "done"
                ok, body = await self.hub.task_status(task_id, handle, status, version)
            if not ok:
                return Reply(f"Task #{task_id}: {body}", ok=False)
            return Reply(f"Task #{task_id} is now {body['task']['status']}.")
        kind = "+1" if action.verb == "up" else "-1"
        ok, body = await self.hub.feed_react(int(action.ref), kind, handle)
        return Reply(f"Reacted {kind}." if ok else f"Not delivered: {body}", ok=ok)
