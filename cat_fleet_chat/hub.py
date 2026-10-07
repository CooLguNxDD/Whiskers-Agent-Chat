"""Business rules. REST, MCP, and the plugin all call this and nothing lower."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from cat_fleet_chat.db import Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat import store
from cat_fleet_chat.validate import (
    CHANNEL_RE,
    TERMINAL_STATUSES,
    attachments,
    channel_name,
    channel_state,
    channel_states,
    clamp_timeout,
    client_request_id,
    destination_name,
    event_kinds,
    fingerprint,
    handle,
    optional_handle,
    optional_id,
    optional_snowflake,
    optional_text,
    origin,
    page_limit,
    parse_mentions,
    require_id,
    require_text,
    task_status,
    transition_allowed,
    wait_timeout,
    webhook_channels,
    webhook_description,
    webhook_direction,
    webhook_format,
    webhook_handles,
    webhook_kinds,
    webhook_name,
    webhook_patterns,
    webhook_url,
)
from cat_fleet_chat.waiters import WaitHub

logger = logging.getLogger("cat_fleet_chat.hub")

SSE_BATCH = 100
# How many parents a reply may climb to find the message that came from Discord.
REPLY_CHAIN_MAX = 5
# A notification call scans at most this many events before it returns the
# advanced cursor, so an agent with no matches cannot pin a read connection.
SCAN_MAX = 2_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flag(value: Any) -> bool:
    return value in (True, 1, "1", "true", "yes")


def _require_writable(channel: dict[str, Any]) -> None:
    """Archived channels keep their history readable but accept no writes."""
    if channel.get("state") == "archived" or channel.get("archived_at"):
        raise HubError(
            "channel_archived",
            f"channel {channel['name']} is archived",
            409,
            {"name": channel["name"], "archived_at": channel["archived_at"]},
        )


def agent_relevant(event: dict[str, Any], agent: str | None) -> bool:
    """Whether ``event`` should notify ``agent``. No agent means every event.

    Mentions and task assignments go to that agent, never for its own action.
    Channel lifecycle events are broadcast.
    """
    if agent is None:
        return True
    kind = event["kind"]
    payload = event.get("payload") or {}
    if kind == "message.created":
        return agent in (payload.get("mentions") or []) and payload.get("author") != agent
    if kind == "task.updated":
        if payload.get("assignee") != agent:
            return False
        history = payload.get("events") or []
        return not history or history[-1].get("actor") != agent
    return kind.startswith("channel.")


class Hub:
    def __init__(self, db: Database, waiters: WaitHub) -> None:
        self.db = db
        self.waiters = waiters
        # Looks up which Discord channel a webhook URL posts to. The app wires
        # in the dispatcher; the hub itself makes no HTTP calls.
        self.discord_resolver: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None

    async def _write(self, fn):
        result = await self.db.write(fn)
        # The writer lock is released before we take the condition lock.
        await self.waiters.notify()
        return result

    async def _txn(self, fn):
        """Run ``fn`` in one immediate transaction, then wake waiters.

        ``fn`` may return early. The commit still happens, and a failure
        rolls the transaction back before the exception leaves this method.
        """

        async def op(conn):
            await conn.execute("BEGIN IMMEDIATE")
            try:
                result = await fn(conn)
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()
            return result

        return await self._write(op)

    @staticmethod
    async def _require_task_writable(conn, task: dict[str, Any]) -> None:
        channel = await store.get_channel_by_id(conn, task["channel_id"])
        if channel is not None:
            _require_writable(channel)

    async def list_channels(
        self, include_archived: Any = False, state: Any = None
    ) -> dict[str, Any]:
        """Channels by name. ``state`` filters (comma list); it can select archived ones too."""
        flag = _flag(include_archived)
        wanted = channel_states(state)

        async def op(conn):
            return await store.list_channels(conn, include_archived=flag, states=wanted)

        return {"channels": await self.db.read(op)}

    async def create_channel(self, name: Any, topic: Any = None) -> dict[str, Any]:
        clean_name = channel_name(name)
        clean_topic = optional_text(topic, "topic", 500)

        async def op(conn):
            existing = await store.get_channel_by_name(conn, clean_name)
            if existing is not None:
                raise HubError(
                    "channel_exists",
                    f"channel {clean_name} already exists",
                    409,
                    {"name": existing["name"], "id": existing["id"]},
                )
            created_at = utc_now()
            channel_id = await store.insert_channel(conn, clean_name, clean_topic, created_at)
            # Read back so the payload carries the column defaults (state="active").
            channel = await store.get_channel_by_id(conn, channel_id)
            await store.insert_event(
                conn,
                "channel.created",
                channel_id,
                channel_id,
                channel,
                created_at,
            )
            return {"channel": channel}

        return await self._txn(op)

    async def _apply_state(
        self,
        conn,
        found: dict[str, Any],
        target: str,
        actor: str,
        note: str,
        forced: bool,
    ) -> dict[str, Any]:
        """Move one channel to ``target`` inside the caller's transaction.

        Entering ``archived`` runs the archive rules: ``fleet`` is refused, and
        unfinished tasks refuse the change unless ``forced``, which cancels
        them first. Leaving ``archived`` reopens writes. The event is
        ``channel.archived``, ``channel.unarchived``, or ``channel.state_changed``.
        """
        current = found["state"]
        if current == target:
            return {
                "channel": found,
                "previous_state": current,
                "cancelled_tasks": [],
                "idempotent": True,
            }
        created_at = utc_now()
        cancelled: list[int] = []
        if target == "archived":
            if found["name"] == "fleet":
                raise HubError("conflict", "the fleet channel cannot be archived", 409)
            unfinished = await store.list_tasks(
                conn,
                channel=found["name"],
                status=None,
                assignee=None,
                exclude_statuses=TERMINAL_STATUSES,
            )
            if unfinished and not forced:
                raise HubError(
                    "channel_has_open_tasks",
                    f"channel {found['name']} has {len(unfinished)} unfinished task(s)",
                    409,
                    {"task_ids": [task["id"] for task in unfinished]},
                )
            reason = f"channel archived: {note}" if note else "channel archived"
            for task in unfinished:
                won = await store.update_task(
                    conn,
                    task["id"],
                    status="cancelled",
                    assignee=task["assignee"],
                    version=task["version"] + 1,
                    updated_at=created_at,
                    expected_version=task["version"],
                )
                if not won:
                    raise HubError(
                        "version_conflict",
                        "a task changed while archiving",
                        409,
                        {"task_id": task["id"]},
                    )
                await store.insert_task_event(
                    conn, task["id"], actor, task["status"], "cancelled", reason, created_at
                )
                fresh_task = await store.get_task(conn, task["id"])
                await store.insert_event(
                    conn, "task.updated", found["id"], task["id"], fresh_task, created_at
                )
                cancelled.append(task["id"])
            kind = "channel.archived"
        elif current == "archived":
            kind = "channel.unarchived"
        else:
            kind = "channel.state_changed"
        await store.update_channel_state(
            conn,
            found["id"],
            state=target,
            note=note,
            updated_at=created_at,
            updated_by=actor,
        )
        fresh = await store.get_channel_by_id(conn, found["id"])
        await store.insert_event(
            conn,
            kind,
            found["id"],
            found["id"],
            {**fresh, "actor": actor, "note": note, "previous_state": current},
            created_at,
        )
        return {
            "channel": fresh,
            "previous_state": current,
            "cancelled_tasks": cancelled,
            "idempotent": False,
        }

    async def set_channel_state(
        self,
        channel: Any,
        state: Any,
        actor: Any,
        note: Any = None,
        force: Any = False,
    ) -> dict[str, Any]:
        """Set a channel's lifecycle state (``validate.CHANNEL_STATES``).

        ``active``/``paused``/``blocked``/``review``/``done`` are labels and do
        not change what the channel accepts. ``archived`` is the read-only
        archive (see :meth:`_apply_state`). Setting the current state is a no-op.
        """
        clean_channel = channel_name(channel)
        target = channel_state(state)
        clean_actor = handle(actor, "actor")
        clean_note = optional_text(note, "note", 2_000)
        forced = _flag(force)

        async def op(conn):
            found = await store.get_channel_by_name(conn, clean_channel)
            if found is None:
                raise HubError("not_found", f"channel {clean_channel} not found", 404)
            return await self._apply_state(conn, found, target, clean_actor, clean_note, forced)

        return await self._txn(op)

    async def archive_channel(
        self,
        channel: Any,
        actor: Any,
        note: Any = None,
        force: Any = False,
    ) -> dict[str, Any]:
        """Shorthand for ``set_channel_state(channel, "archived", ...)``."""
        return await self.set_channel_state(channel, "archived", actor, note=note, force=force)

    async def unarchive_channel(
        self, channel: Any, actor: Any, state: Any = "active"
    ) -> dict[str, Any]:
        """Reopen an archived channel into ``state`` (default ``active``).

        A channel that is not archived is left as is. Cancelled tasks stay cancelled.
        """
        clean_channel = channel_name(channel)
        clean_actor = handle(actor, "actor")
        target = channel_state(state or "active")
        if target == "archived":
            raise HubError(
                "validation_error", "unarchive needs a state other than archived", 400,
                {"field": "state"},
            )

        async def op(conn):
            found = await store.get_channel_by_name(conn, clean_channel)
            if found is None:
                raise HubError("not_found", f"channel {clean_channel} not found", 404)
            if found["state"] != "archived":
                return {"channel": found, "previous_state": found["state"], "idempotent": True}
            return await self._apply_state(conn, found, target, clean_actor, "", False)

        return await self._txn(op)

    async def post_message(
        self,
        channel: Any,
        author: Any,
        text: Any,
        reply_to: Any = None,
        client_request_id_value: Any = None,
        attachments_value: Any = None,
        destination_value: Any = None,
        origin_value: Any = None,
    ) -> dict[str, Any]:
        """Post a message.

        ``destination`` addresses it to an outbound webhook (a Discord channel).
        With no destination and a ``reply_to`` whose thread started in Discord,
        the hub addresses the reply to the hook attached to that Discord
        channel. ``origin`` says where the message came from; only the relay sets it.
        """
        clean_channel = channel_name(channel)
        clean_author = handle(author, "author")
        files = attachments(attachments_value)
        clean_text = require_text(text, "text", 16_000)
        reply = optional_id(reply_to, "reply_to")
        if reply == 0:
            reply = None
        key = client_request_id(client_request_id_value)
        explicit_destination = destination_name(destination_value)
        clean_origin = origin(origin_value)
        mentions = parse_mentions(clean_text)
        payload = {
            "channel": clean_channel,
            "author": clean_author,
            "text": clean_text,
            "reply_to": reply,
        }
        if files:
            # Only added when present so pre-attachment idempotency keys still match.
            payload["attachments"] = files
        if explicit_destination:
            payload["destination"] = explicit_destination
        if clean_origin:
            payload["origin"] = clean_origin
        digest = fingerprint("message", payload) if key else None

        async def op(conn):
            if key and digest is not None:
                prior = await store.get_idempotency(conn, key)
                if prior is not None:
                    if prior["fingerprint"] != digest or prior["kind"] != "message":
                        raise HubError(
                            "idempotency_conflict",
                            "client_request_id was already used with a different payload",
                            409,
                            {"client_request_id": key},
                        )
                    return prior["result"]
            found = await store.get_channel_by_name(conn, clean_channel)
            if found is None:
                raise HubError("not_found", f"channel {clean_channel} not found", 404)
            _require_writable(found)
            parent = None
            if reply is not None:
                parent = await store.get_message(conn, reply)
                if parent is None or parent["channel_id"] != found["id"]:
                    raise HubError(
                        "validation_error",
                        "reply_to must be a message in the same channel",
                        400,
                        {"field": "reply_to"},
                    )
            destination, warnings = await self._resolve_destination(conn, explicit_destination, parent)
            created_at = utc_now()
            message_id = await store.insert_message(
                conn,
                found["id"],
                clean_author,
                clean_text,
                reply,
                created_at,
                mentions,
                files,
                destination=destination,
                origin=clean_origin,
            )
            message = await store.get_message(conn, message_id)
            assert message is not None
            await store.insert_event(
                conn,
                "message.created",
                found["id"],
                message_id,
                message,
                created_at,
            )
            result: dict[str, Any] = {"message": message}
            if warnings:
                result["warnings"] = warnings
            if key and digest is not None:
                await store.insert_idempotency(conn, key, "message", digest, result, created_at)
            return result

        return await self._txn(op)

    @staticmethod
    async def _resolve_destination(
        conn, explicit: str | None, parent: dict[str, Any] | None
    ) -> tuple[str | None, list[str]]:
        """The outbound hook a new message goes to, plus warnings for the poster.

        An explicit name must be an enabled outbound hook. Otherwise a reply
        inherits the destination of the Discord channel its thread started in.
        """
        if explicit is not None:
            hook = await store.get_webhook(conn, explicit)
            if hook is None or hook["direction"] != "out":
                names = [
                    h["name"] for h in await store.list_webhooks(conn, direction="out", enabled_only=True)
                ]
                raise HubError(
                    "destination_not_found",
                    f"destination {explicit} is not an outbound webhook",
                    404,
                    {"destinations": names},
                )
            if not hook["enabled"]:
                raise HubError(
                    "destination_disabled",
                    f"destination {explicit} is disabled",
                    409,
                    {"destination": explicit},
                )
            return explicit, []
        if parent is None:
            return None, []
        # Walk up the reply chain for the message that came from Discord.
        current: dict[str, Any] | None = parent
        origin_found = None
        for _ in range(REPLY_CHAIN_MAX):
            if current is None:
                break
            if current.get("origin"):
                origin_found = current["origin"]
                break
            reply_to = current.get("reply_to")
            current = await store.get_message(conn, reply_to) if reply_to else None
        if origin_found is None:
            return None, []
        hook = await store.find_destination_hook(conn, origin_found["channel_id"])
        if hook is None:
            return None, [
                f"no destination is attached for Discord channel {origin_found['channel_id']}; "
                "this reply stays in the hub"
            ]
        return hook["name"], []

    async def get_messages(
        self,
        channel: Any,
        *,
        since_id: Any = None,
        before_id: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        clean_channel = channel_name(channel)
        since = optional_id(since_id, "since_id")
        before = optional_id(before_id, "before_id")
        if since is not None and before is not None:
            raise HubError(
                "validation_error",
                "since_id and before_id cannot be combined",
                400,
            )
        clean_limit = page_limit(limit)

        async def op(conn):
            found = await store.get_channel_by_name(conn, clean_channel)
            if found is None:
                raise HubError("not_found", f"channel {clean_channel} not found", 404)
            messages, has_more = await store.list_messages(
                conn,
                found["id"],
                since_id=since,
                before_id=before,
                limit=clean_limit,
            )
            event_cursor = await store.max_event_id(conn)
            return messages, has_more, event_cursor

        messages, has_more, event_cursor = await self.db.read(op)
        if messages:
            cursor = messages[-1]["id"] if since is not None else messages[-1]["id"]
            next_before = messages[0]["id"]
        else:
            cursor = since if since is not None else 0
            next_before = before
        return {
            "messages": messages,
            "cursor": cursor,
            "next_before_id": next_before,
            "has_more": has_more,
            "event_cursor": event_cursor,
        }

    async def read_mentions(
        self,
        agent: str,
        since_id: int,
        *,
        channel: str | None,
        limit: int,
    ) -> dict[str, Any]:
        async def op(conn):
            if channel is not None:
                found = await store.get_channel_by_name(conn, channel)
                if found is None:
                    raise HubError("not_found", f"channel {channel} not found", 404)
            messages, has_more = await store.mentions_after(
                conn, agent, since_id, channel=channel, limit=limit
            )
            return messages, has_more

        messages, has_more = await self.db.read(op)
        cursor = messages[-1]["id"] if messages else since_id
        return {
            "messages": messages,
            "timed_out": False,
            "cursor": cursor,
            "has_more": has_more,
            "channel": channel,
        }

    async def wait_for_mentions(
        self,
        agent: Any,
        since_id: Any = 0,
        *,
        channel: Any = None,
        timeout: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Park until ``agent`` is mentioned after ``since_id``, or the deadline.

        The condition lock is held across the empty check and the sleep. A
        commit that happens in between blocks on the same lock, then notifies,
        so the wake cannot be lost. The deadline is monotonic and is not
        extended by unrelated notifications. Cancellation leaves the waiter.
        """
        import asyncio
        import time

        clean_agent = handle(agent, "agent")
        cursor = optional_id(since_id, "since_id") or 0
        clean_channel = channel_name(channel) if channel not in (None, "") else None
        seconds = clamp_timeout(timeout)
        clean_limit = page_limit(limit)
        deadline = time.monotonic() + seconds

        async with self.waiters.condition:
            while True:
                page = await self.read_mentions(
                    clean_agent, cursor, channel=clean_channel, limit=clean_limit
                )
                if page["messages"]:
                    return page
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    final = await self.read_mentions(
                        clean_agent, cursor, channel=clean_channel, limit=clean_limit
                    )
                    if final["messages"]:
                        return final
                    final["timed_out"] = True
                    final["cursor"] = cursor
                    return final
                try:
                    await asyncio.wait_for(self.waiters.condition.wait(), remaining)
                except asyncio.TimeoutError:
                    continue

    async def create_task(
        self,
        channel: Any,
        title: Any,
        description: Any = None,
        assignee: Any = None,
        actor: Any = None,
        client_request_id_value: Any = None,
    ) -> dict[str, Any]:
        clean_channel = channel_name(channel)
        clean_title = require_text(title, "title", 200)
        clean_description = optional_text(description, "description", 16_000)
        clean_assignee = optional_handle(assignee, "assignee")
        # The actor is the person recording the create. Default it to the
        # assignee when a caller only sent one handle (the MCP create tool).
        raw_actor = actor if actor not in (None, "") else clean_assignee
        clean_actor = handle(raw_actor, "actor")
        status = "claimed" if clean_assignee else "open"
        key = client_request_id(client_request_id_value)
        payload = {
            "channel": clean_channel,
            "title": clean_title,
            "description": clean_description,
            "assignee": clean_assignee,
            "actor": clean_actor,
        }
        digest = fingerprint("task", payload) if key else None

        async def op(conn):
            if key and digest is not None:
                prior = await store.get_idempotency(conn, key)
                if prior is not None:
                    if prior["fingerprint"] != digest or prior["kind"] != "task":
                        raise HubError(
                            "idempotency_conflict",
                            "client_request_id was already used with a different payload",
                            409,
                            {"client_request_id": key},
                        )
                    return prior["result"]
            found = await store.get_channel_by_name(conn, clean_channel)
            if found is None:
                raise HubError("not_found", f"channel {clean_channel} not found", 404)
            _require_writable(found)
            created_at = utc_now()
            task_id = await store.insert_task(
                conn,
                found["id"],
                clean_title,
                clean_description,
                status,
                clean_assignee,
                created_at,
            )
            await store.insert_task_event(
                conn, task_id, clean_actor, None, status, "", created_at
            )
            task = await store.get_task(conn, task_id)
            assert task is not None
            await store.insert_event(
                conn, "task.updated", found["id"], task_id, task, created_at
            )
            result = {"task": task}
            if key and digest is not None:
                await store.insert_idempotency(conn, key, "task", digest, result, created_at)
            return result

        return await self._txn(op)

    async def claim_task(
        self,
        task_id: Any,
        actor: Any,
        expected_version: Any = None,
    ) -> dict[str, Any]:
        clean_id = require_id(task_id, "task_id")
        clean_actor = handle(actor, "actor")
        version = optional_id(expected_version, "expected_version")

        async def op(conn):
            current = await store.get_task(conn, clean_id)
            if current is None:
                raise HubError("not_found", f"task {clean_id} not found", 404)
            await self._require_task_writable(conn, current)
            if version is not None and current["version"] != version:
                raise HubError(
                    "version_conflict",
                    "task version does not match",
                    409,
                    {"task": current},
                )
            if current["status"] == "claimed" and current["assignee"] == clean_actor:
                return {"task": current, "idempotent": True}
            if current["status"] != "open" or current["assignee"] is not None:
                raise HubError(
                    "conflict",
                    "task is not an unassigned open task",
                    409,
                    {"task": current},
                )
            created_at = utc_now()
            won = await store.claim_open_task(conn, clean_id, clean_actor, created_at, version)
            if not won:
                raced = await store.get_task(conn, clean_id)
                if raced and raced["assignee"] == clean_actor and raced["status"] == "claimed":
                    return {"task": raced, "idempotent": True}
                raise HubError(
                    "conflict",
                    "task was claimed by someone else",
                    409,
                    {"task": raced},
                )
            await store.insert_task_event(
                conn, clean_id, clean_actor, "open", "claimed", "", created_at
            )
            task = await store.get_task(conn, clean_id)
            assert task is not None
            await store.insert_event(
                conn, "task.updated", task["channel_id"], clean_id, task, created_at
            )
            return {"task": task, "idempotent": False}

        return await self._txn(op)

    async def update_task_status(
        self,
        task_id: Any,
        status: Any,
        actor: Any,
        expected_version: Any,
        note: Any = None,
    ) -> dict[str, Any]:
        clean_id = require_id(task_id, "task_id")
        target = task_status(status)
        clean_actor = handle(actor, "actor")
        version = require_id(expected_version, "expected_version")
        clean_note = optional_text(note, "note", 2_000)

        async def op(conn):
            current = await store.get_task(conn, clean_id)
            if current is None:
                raise HubError("not_found", f"task {clean_id} not found", 404)
            await self._require_task_writable(conn, current)
            if current["version"] != version:
                raise HubError(
                    "version_conflict",
                    "task version does not match",
                    409,
                    {"task": current},
                )
            if not transition_allowed(current["status"], target):
                raise HubError(
                    "invalid_transition",
                    f"cannot move {current['status']} to {target}",
                    409,
                    {"task": current},
                )
            assignee = None if target == "open" else current["assignee"]
            if target == "claimed" and not assignee:
                assignee = clean_actor
            created_at = utc_now()
            won = await store.update_task(
                conn,
                clean_id,
                status=target,
                assignee=assignee,
                version=version + 1,
                updated_at=created_at,
                expected_version=version,
            )
            if not won:
                raced = await store.get_task(conn, clean_id)
                raise HubError(
                    "version_conflict",
                    "task version does not match",
                    409,
                    {"task": raced},
                )
            await store.insert_task_event(
                conn,
                clean_id,
                clean_actor,
                current["status"],
                target,
                clean_note,
                created_at,
            )
            task = await store.get_task(conn, clean_id)
            assert task is not None
            await store.insert_event(
                conn, "task.updated", task["channel_id"], clean_id, task, created_at
            )
            return {"task": task}

        return await self._txn(op)

    async def list_tasks(
        self,
        *,
        channel: Any = None,
        status: Any = None,
        assignee: Any = None,
    ) -> dict[str, Any]:
        clean_channel = channel_name(channel) if channel not in (None, "") else None
        clean_status = task_status(status) if status not in (None, "") else None
        clean_assignee = handle(assignee, "assignee") if assignee not in (None, "") else None

        async def op(conn):
            if clean_channel is not None:
                found = await store.get_channel_by_name(conn, clean_channel)
                if found is None:
                    raise HubError("not_found", f"channel {clean_channel} not found", 404)
            return await store.list_tasks(
                conn,
                channel=clean_channel,
                status=clean_status,
                assignee=clean_assignee,
            )

        tasks = await self.db.read(op)
        return {"tasks": tasks}

    async def get_attachment(self, attachment_id: Any) -> dict[str, Any]:
        clean_id = require_id(attachment_id, "attachment_id")

        async def op(conn):
            return await store.get_attachment(conn, clean_id)

        found = await self.db.read(op)
        if found is None:
            raise HubError("not_found", f"attachment {clean_id} not found", 404)
        return {"attachment": found}

    # ---- webhooks -------------------------------------------------------

    @staticmethod
    def _hook_ident(value: Any) -> int | str:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return webhook_name(value)

    async def _require_hook(self, conn, ident: Any) -> dict[str, Any]:
        found = await store.get_webhook(conn, self._hook_ident(ident))
        if found is None:
            raise HubError("not_found", f"webhook {ident} not found", 404)
        return found

    async def list_webhooks(self, direction: Any = None) -> dict[str, Any]:
        wanted = webhook_direction(direction) if direction not in (None, "") else None

        async def op(conn):
            return await store.list_webhooks(conn, direction=wanted)

        return {"webhooks": [store.public_webhook(h) for h in await self.db.read(op)]}

    async def get_webhook(self, ident: Any) -> dict[str, Any]:
        async def op(conn):
            return await self._require_hook(conn, ident)

        return {"webhook": store.public_webhook(await self.db.read(op))}

    async def create_webhook(
        self,
        name: Any,
        direction: Any,
        *,
        url: Any = None,
        format: Any = None,
        kinds: Any = None,
        channels: Any = None,
        mentions: Any = None,
        exclude_authors: Any = None,
        channel: Any = None,
        author: Any = None,
        allow_override: Any = False,
        description: Any = None,
        directed_only: Any = False,
        discord_channel_id: Any = None,
    ) -> dict[str, Any]:
        """Create a webhook. The generated ``secret`` is returned once, here only.

        An outbound hook is also a destination agents can address by name.
        ``discord_channel_id`` links it to a Discord channel so replies to
        messages from that channel return to it; for a Discord hook the hub
        looks it up itself when it is not given.
        """
        clean_name = webhook_name(name)
        clean_direction = webhook_direction(direction)
        values: dict[str, Any] = {"name": clean_name, "direction": clean_direction}
        if clean_direction == "out":
            clean_url = webhook_url(url)
            clean_format = webhook_format(format)
            linked_channel = optional_snowflake(discord_channel_id, "discord_channel_id")
            if linked_channel is None and clean_format == "discord":
                linked_channel = await self._resolve_discord_channel(clean_url)
            values.update(
                url=clean_url,
                format=clean_format,
                kinds=webhook_kinds(kinds),
                channels=webhook_channels(channels),
                mentions=webhook_handles(mentions, "mentions"),
                exclude_authors=webhook_patterns(exclude_authors, "exclude_authors"),
                description=webhook_description(description),
                directed_only=int(_flag(directed_only)),
                discord_channel_id=linked_channel,
            )
        else:
            values.update(
                channel=channel_name(channel),
                author=(
                    handle(author, "author")
                    if author not in (None, "")
                    else f"hook-{clean_name}"[:64]
                ),
                allow_override=int(_flag(allow_override)),
            )
        secret = secrets.token_urlsafe(32)

        async def op(conn):
            if await store.get_webhook(conn, clean_name) is not None:
                raise HubError(
                    "webhook_exists", f"webhook {clean_name} already exists", 409, {"name": clean_name}
                )
            if clean_direction == "in":
                if await store.get_channel_by_name(conn, values["channel"]) is None:
                    raise HubError("not_found", f"channel {values['channel']} not found", 404)
            else:
                # A new outbound hook starts at the end of the log: no history replay.
                values["cursor"] = await store.max_event_id(conn)
            now = utc_now()
            hook_id = await store.insert_webhook(
                conn, {**values, "secret": secret, "created_at": now, "updated_at": now}
            )
            return await store.get_webhook(conn, hook_id)

        # _txn wakes waiters, which also makes the dispatcher reload its hooks.
        hook = await self._txn(op)
        return {"webhook": store.public_webhook(hook), "secret": secret}

    async def update_webhook(self, ident: Any, changes: dict[str, Any]) -> dict[str, Any]:
        """Change filters, target or ``enabled``. ``name``, ``direction`` and the secret are fixed."""
        if not isinstance(changes, dict):
            raise HubError("validation_error", "changes must be an object", 400)

        # A new URL may point at a different Discord channel, so the old link is
        # stale. Look the new one up before the transaction: it is a network call.
        relink = False
        relinked_channel: str | None = None
        if "url" in changes and "discord_channel_id" not in changes:
            current = await self.webhook_with_secret(ident)
            if current["direction"] == "out":
                relink = True
                new_format = (
                    webhook_format(changes["format"]) if "format" in changes else current["format"]
                )
                if new_format == "discord":
                    relinked_channel = await self._resolve_discord_channel(webhook_url(changes["url"]))

        async def op(conn):
            hook = await self._require_hook(conn, ident)
            outbound = hook["direction"] == "out"
            allowed = (
                {
                    "enabled",
                    "url",
                    "format",
                    "kinds",
                    "channels",
                    "mentions",
                    "exclude_authors",
                    "description",
                    "directed_only",
                    "discord_channel_id",
                }
                if outbound
                else {"enabled", "channel", "author", "allow_override"}
            )
            unknown = sorted(set(changes) - allowed)
            if unknown:
                raise HubError(
                    "validation_error",
                    f"cannot change on a {hook['direction']} webhook: {', '.join(unknown)}",
                    400,
                    {"allowed": sorted(allowed)},
                )
            values: dict[str, Any] = {}
            if "enabled" in changes:
                values["enabled"] = int(_flag(changes["enabled"]))
                if values["enabled"] and outbound and not hook["enabled"]:
                    # Re-enabling clears the failure that disabled it.
                    values.update(failure_count=0, last_error=None)
            if "url" in changes:
                values["url"] = webhook_url(changes["url"])
            if "format" in changes:
                values["format"] = webhook_format(changes["format"])
            if "kinds" in changes:
                values["kinds"] = webhook_kinds(changes["kinds"])
            if "channels" in changes:
                values["channels"] = webhook_channels(changes["channels"])
            if "mentions" in changes:
                values["mentions"] = webhook_handles(changes["mentions"], "mentions")
            if "exclude_authors" in changes:
                values["exclude_authors"] = webhook_patterns(
                    changes["exclude_authors"], "exclude_authors"
                )
            if "description" in changes:
                values["description"] = webhook_description(changes["description"])
            if "directed_only" in changes:
                values["directed_only"] = int(_flag(changes["directed_only"]))
            if "discord_channel_id" in changes:
                values["discord_channel_id"] = optional_snowflake(
                    changes["discord_channel_id"], "discord_channel_id"
                )
            elif relink:
                # None clears a link that the new URL may no longer match.
                values["discord_channel_id"] = relinked_channel
            if "channel" in changes:
                target = channel_name(changes["channel"])
                if await store.get_channel_by_name(conn, target) is None:
                    raise HubError("not_found", f"channel {target} not found", 404)
                values["channel"] = target
            if "author" in changes:
                values["author"] = handle(changes["author"], "author")
            if "allow_override" in changes:
                values["allow_override"] = int(_flag(changes["allow_override"]))
            if values:
                values["updated_at"] = utc_now()
                await store.update_webhook(conn, hook["id"], values)
            return await store.get_webhook(conn, hook["id"])

        return {"webhook": store.public_webhook(await self._txn(op))}

    async def _resolve_discord_channel(self, url: str) -> str | None:
        """The Discord channel id a webhook URL posts to, or ``None``. Never raises."""
        if self.discord_resolver is None:
            return None
        try:
            info = await self.discord_resolver(url)
        except Exception:  # a failed lookup must not block creating the hook
            logger.warning("could not look up the Discord channel for a webhook", exc_info=True)
            return None
        channel_id = (info or {}).get("channel_id")
        return str(channel_id) if channel_id else None

    async def list_destinations(self) -> dict[str, Any]:
        """Outbound hooks agents can address by name. No URL, no secret."""

        async def op(conn):
            return await store.list_webhooks(conn, direction="out", enabled_only=True)

        return {
            "destinations": [
                {
                    "name": hook["name"],
                    "description": hook["description"],
                    "format": hook["format"],
                    "discord_channel_id": hook["discord_channel_id"],
                    "directed_only": hook["directed_only"],
                }
                for hook in await self.db.read(op)
            ]
        }

    async def message_context(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        """Author, text and origin of the given messages, for quoting a reply."""

        async def op(conn):
            found: dict[int, dict[str, Any]] = {}
            for message_id in ids:
                message = await store.get_message(conn, message_id)
                if message is not None:
                    found[message_id] = {
                        "author": message["author"],
                        "text": message["text"],
                        "origin": message["origin"],
                    }
            return found

        return await self.db.read(op)

    async def rotate_webhook_secret(self, ident: Any) -> dict[str, Any]:
        secret = secrets.token_urlsafe(32)

        async def op(conn):
            hook = await self._require_hook(conn, ident)
            await store.update_webhook(conn, hook["id"], {"secret": secret, "updated_at": utc_now()})
            return await store.get_webhook(conn, hook["id"])

        hook = await self._txn(op)
        return {"webhook": store.public_webhook(hook), "secret": secret}

    async def delete_webhook(self, ident: Any) -> dict[str, Any]:
        async def op(conn):
            hook = await self._require_hook(conn, ident)
            await store.delete_webhook(conn, hook["id"])
            return {"deleted": True, "id": hook["id"], "name": hook["name"]}

        return await self._txn(op)

    async def webhook_secret_hook(self, name: Any, direction: str) -> dict[str, Any] | None:
        """The full row (with secret) for an enabled hook, or ``None``. For the dispatcher and the inbound route."""
        if not isinstance(name, str) or not CHANNEL_RE.fullmatch(name):
            return None

        async def op(conn):
            return await store.get_webhook(conn, name)

        hook = await self.db.read(op)
        if hook is None or not hook["enabled"] or hook["direction"] != direction:
            return None
        return hook

    async def webhook_with_secret(self, ident: Any) -> dict[str, Any]:
        """Full row including the secret, for sending a test ping. Never return this from an API."""

        async def op(conn):
            return await self._require_hook(conn, ident)

        return await self.db.read(op)

    async def outbound_webhooks(self) -> list[dict[str, Any]]:
        """Enabled outbound hooks, full rows with secrets. Internal to the dispatcher."""

        async def op(conn):
            return await store.list_webhooks(conn, direction="out", enabled_only=True)

        return await self.db.read(op)

    async def record_delivery(
        self,
        hook_id: int,
        *,
        status: str | None,
        cursor: int | None = None,
        error: str | None = None,
        failure_count: int = 0,
        disable: bool = False,
    ) -> None:
        """Persist the outcome of a delivery attempt.

        ``status=None`` moves only the cursor (nothing was sent), leaving the
        last delivery's status and error alone. Writes straight through the
        database, without waking waiters, so delivery bookkeeping never
        triggers the dispatcher or an SSE reader.
        """
        values: dict[str, Any] = {"failure_count": failure_count}
        if status is not None:
            values.update(last_status=status, last_error=error, last_delivery_at=utc_now())
        if cursor is not None:
            values["cursor"] = cursor
        if disable:
            values["enabled"] = 0

        async def op(conn):
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await store.update_webhook(conn, hook_id, values)
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()

        await self.db.write(op)

    async def post_via_webhook(self, hook: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        """Post for an authenticated inbound hook.

        The hook fixes the channel and author unless ``allow_override`` is set.
        ``content`` is accepted as an alias of ``text`` for Discord-shaped senders.
        """
        channel = hook["channel"]
        author = hook["author"]
        if hook["allow_override"]:
            channel = body.get("channel") or channel
            author = body.get("author") or author
        key = client_request_id(body.get("client_request_id"))
        if key is not None and len(key) > 120:
            raise HubError("validation_error", "client_request_id must be 1..120 characters", 400)
        text = body.get("text")
        if text in (None, ""):
            text = body.get("content")
        return await self.post_message(
            channel,
            author,
            text,
            reply_to=body.get("reply_to"),
            # Namespaced so two hooks cannot collide on the global idempotency key.
            client_request_id_value=f"hook:{hook['name']}:{key}" if key else None,
        )

    async def event_cursor(self) -> dict[str, Any]:
        return {"event_cursor": await self.db.read(store.max_event_id)}

    async def scan_events(
        self,
        after_event_id: int,
        *,
        agent: str | None,
        kinds: frozenset[str] | None,
        channel: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Relevant events after the cursor, the advanced cursor, and ``has_more``.

        Events that do not match ``agent`` still advance the cursor, so the
        next call does not scan them again. When ``limit`` matches are found
        the cursor stops at the last one returned.
        """
        cursor = after_event_id
        found: list[dict[str, Any]] = []
        scanned = 0
        while True:
            batch = await self.events_after(cursor, channel=channel, kinds=kinds)
            for event in batch:
                cursor = int(event["id"])
                scanned += 1
                if agent_relevant(event, agent):
                    found.append(event)
                    if len(found) >= limit:
                        return found, cursor, True
            if len(batch) < SSE_BATCH:
                return found, cursor, False
            if scanned >= SCAN_MAX:
                return found, cursor, True

    async def wait_for_events(
        self,
        agent: Any = None,
        after_event_id: Any = 0,
        *,
        kinds: Any = None,
        channel: Any = None,
        timeout: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Park until an event relevant to ``agent`` lands after the cursor.

        Same lock discipline as :meth:`wait_for_mentions`. ``timeout=0``
        returns at once. The returned ``cursor`` is always safe to send back,
        including on timeout, and skips events that were not relevant.
        """
        import asyncio
        import time

        clean_agent = handle(agent, "agent") if agent not in (None, "") else None
        cursor = optional_id(after_event_id, "after_event_id") or 0
        clean_kinds = event_kinds(kinds)
        clean_channel = channel_name(channel) if channel not in (None, "") else None
        seconds = wait_timeout(timeout)
        clean_limit = page_limit(limit)
        deadline = time.monotonic() + seconds
        if clean_channel is not None:
            await self._require_channel(clean_channel)

        def page(found: list[dict[str, Any]], at: int, more: bool, timed_out: bool):
            return {
                "events": found,
                "cursor": at,
                "has_more": more,
                "timed_out": timed_out,
                "agent": clean_agent,
            }

        async with self.waiters.condition:
            while True:
                found, cursor, more = await self.scan_events(
                    cursor,
                    agent=clean_agent,
                    kinds=clean_kinds,
                    channel=clean_channel,
                    limit=clean_limit,
                )
                if found or more:
                    return page(found, cursor, more, False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return page([], cursor, False, seconds > 0)
                try:
                    await asyncio.wait_for(self.waiters.condition.wait(), remaining)
                except asyncio.TimeoutError:
                    continue

    async def _require_channel(self, name: str) -> None:
        async def op(conn):
            return await store.get_channel_by_name(conn, name)

        if await self.db.read(op) is None:
            raise HubError("not_found", f"channel {name} not found", 404)

    async def list_agents(self) -> dict[str, Any]:
        agents = await self.db.read(store.list_agents)
        return {"agents": agents, "label": "recent activity"}

    async def events_after(
        self,
        after_event_id: int,
        *,
        channel: str | None,
        limit: int = SSE_BATCH,
        kinds: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        async def op(conn):
            return await store.events_after(
                conn, after_event_id, channel=channel, limit=limit, kinds=kinds
            )

        return await self.db.read(op)

    async def cursor_expired(self, after_event_id: int) -> bool:
        """True when ``after_event_id`` pointed at a row this process no longer has.

        V1 never deletes events, so this stays false. A future retention pass
        must surface it as an SSE ``reset`` instead of silently skipping.
        """
        if after_event_id <= 0:
            return False

        async def op(conn):
            if await store.event_exists(conn, after_event_id):
                return False
            earliest = await store.min_event_id(conn)
            if earliest is None:
                return True
            return earliest > after_event_id

        return await self.db.read(op)
