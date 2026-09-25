"""Business rules. REST, MCP, and the plugin all call this and nothing lower."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cat_fleet_chat.db import Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat import store
from cat_fleet_chat.validate import (
    TERMINAL_STATUSES,
    attachments,
    channel_name,
    channel_state,
    channel_states,
    clamp_timeout,
    client_request_id,
    event_kinds,
    fingerprint,
    handle,
    optional_handle,
    optional_id,
    optional_text,
    page_limit,
    parse_mentions,
    require_id,
    require_text,
    task_status,
    transition_allowed,
    wait_timeout,
)
from cat_fleet_chat.waiters import WaitHub

SSE_BATCH = 100
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
    ) -> dict[str, Any]:
        clean_channel = channel_name(channel)
        clean_author = handle(author, "author")
        files = attachments(attachments_value)
        clean_text = require_text(text, "text", 16_000)
        reply = optional_id(reply_to, "reply_to")
        if reply == 0:
            reply = None
        key = client_request_id(client_request_id_value)
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
            if reply is not None:
                parent = await store.get_message(conn, reply)
                if parent is None or parent["channel_id"] != found["id"]:
                    raise HubError(
                        "validation_error",
                        "reply_to must be a message in the same channel",
                        400,
                        {"field": "reply_to"},
                    )
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
            result = {"message": message}
            if key and digest is not None:
                await store.insert_idempotency(conn, key, "message", digest, result, created_at)
            return result

        return await self._txn(op)

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
