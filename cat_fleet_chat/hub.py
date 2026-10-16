"""Business rules. REST, MCP, and the plugin all call this and nothing lower."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from cat_fleet_chat.db import Database
from cat_fleet_chat.errors import HubError
from cat_fleet_chat import embed, feed, knowledge, mailbox, store, validate
from cat_fleet_chat import search as search_rules
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
from cat_fleet_chat.validate import client_request_id as client_request_id_fn
from cat_fleet_chat.validate import handle as validate_handle
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
    Mail goes to its recipient, including mail an agent sent itself.
    Channel lifecycle events are broadcast.
    """
    if agent is None:
        return True
    kind = event["kind"]
    payload = event.get("payload") or {}
    if kind == "mail":
        return payload.get("recipient") == agent
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
        # Awaited after a new feed post commits, with the post result. See feed_post.
        self.feed_post_hooks: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        # Optional embeddings. The app swaps in the configured provider and
        # starts the queue; without them vector and hybrid search answer
        # not_available and nothing else changes.
        self.embedder: embed.Embedder = embed.NullEmbedder()
        self.embed_queue: embed.EmbedQueue | None = None
        # Index entries touched by the running write transaction. They go to
        # the embed queue only after the commit, so it never reads stale text.
        self._reindexed: list[tuple[str, int]] = []

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
            self._reindexed = []
            await conn.execute("BEGIN IMMEDIATE")
            try:
                result = await fn(conn)
            except BaseException:
                self._reindexed = []
                await conn.rollback()
                raise
            await conn.commit()
            touched, self._reindexed = self._reindexed, []
            if touched and self.embed_queue is not None:
                self.embed_queue.mark(touched)
            return result

        return await self._write(op)

    @staticmethod
    async def _require_task_writable(conn, task: dict[str, Any]) -> None:
        channel = await store.get_channel_by_id(conn, task["channel_id"])
        if channel is not None:
            _require_writable(channel)

    async def _index(self, conn, kind: str, ref_id: int) -> None:
        """Refresh the search entry for one row, inside the caller's write transaction."""
        if not self.db.fts5_available:
            return
        if kind == "message":
            await store.index_message(conn, ref_id)
        else:
            await store.reindex(conn, kind, ref_id)
        if self.embed_queue is not None:
            # Attachments of a message are re-embedded when their own entry
            # changes; the missing-vector scan picks up new ones.
            self._reindexed.append((kind, ref_id))

    async def _index_knowledge_batch(self, conn, fact_ids: list[int]) -> None:
        """Refresh search index entries for multiple knowledge facts in one write transaction."""
        if not fact_ids or not self.db.fts5_available:
            return
        await store.index_knowledge_batch(conn, fact_ids)
        if self.embed_queue is not None:
            self._reindexed.extend(("knowledge", fid) for fid in fact_ids)

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
                await self._index(conn, "task", task["id"])
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
            await self._index(conn, "message", message_id)
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

    async def read_mail_after(
        self, agent: str, since_id: int, limit: int, *, with_unread: bool = True
    ) -> dict[str, Any]:
        """Mail for ``agent`` after the mail cursor, plus the unread count. For wait responses.

        ``with_unread=False`` leaves ``mail_unread`` out: the wait loop checks
        on every wake and needs the count only once, when it answers.
        """

        async def op(conn):
            found, has_more = await store.list_mail(
                conn, agent, state="all", since_id=since_id, limit=limit
            )
            unread = await store.count_unread_mail(conn, agent) if with_unread else None
            return found, has_more, unread

        found, has_more, unread = await self.db.read(op)
        page: dict[str, Any] = {
            "mail": found,
            "mail_cursor": found[-1]["id"] if found else since_id,
            "mail_has_more": has_more,
        }
        if unread is not None:
            page["mail_unread"] = unread
        return page

    async def wait_for_mentions(
        self,
        agent: Any,
        since_id: Any = 0,
        *,
        channel: Any = None,
        timeout: Any = None,
        limit: Any = None,
        mail_since_id: Any = None,
    ) -> dict[str, Any]:
        """Park until ``agent`` is mentioned after ``since_id`` or gets mail, or the deadline.

        The condition lock is held across the empty check and the sleep. A
        commit that happens in between blocks on the same lock, then notifies,
        so the wake cannot be lost. The deadline is monotonic and is not
        extended by unrelated notifications. Cancellation leaves the waiter.

        Mail after ``mail_since_id`` wakes the waiter too; send back the
        returned ``mail_cursor``. Without ``mail_since_id`` only mail that
        arrives during this call wakes it, so a caller that never reads its
        mail is not woken again and again by the same unread mail.
        ``mail_unread`` always says how much is waiting.
        """
        import asyncio
        import time

        clean_agent = handle(agent, "agent")
        cursor = optional_id(since_id, "since_id") or 0
        clean_channel = channel_name(channel) if channel not in (None, "") else None
        seconds = clamp_timeout(timeout)
        clean_limit = page_limit(limit)
        explicit_mail = optional_id(mail_since_id, "mail_since_id")
        deadline = time.monotonic() + seconds

        async def check() -> dict[str, Any]:
            page = await self.read_mentions(
                clean_agent, cursor, channel=clean_channel, limit=clean_limit
            )
            page.update(
                await self.read_mail_after(clean_agent, mail_cursor, clean_limit, with_unread=False)
            )
            return page

        async def answer(page: dict[str, Any]) -> dict[str, Any]:
            page["mail_unread"] = await self.db.read(
                lambda conn: store.count_unread_mail(conn, clean_agent)
            )
            return page

        async with self.waiters.condition:
            # Taken under the lock: mail committed after this point notifies us.
            if explicit_mail is not None:
                mail_cursor = explicit_mail
            else:
                mail_cursor = await self.db.read(
                    lambda conn: store.max_mail_id(conn, clean_agent)
                )
            while True:
                page = await check()
                if page["messages"] or page["mail"]:
                    return await answer(page)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # The check above found nothing, and a commit after it
                    # would have waited on this lock; answer with the cursors
                    # we hold instead of reading everything again.
                    page["timed_out"] = True
                    page["cursor"] = cursor
                    page["mail_cursor"] = mail_cursor
                    return await answer(page)
                try:
                    await asyncio.wait_for(self.waiters.condition.wait(), remaining)
                except asyncio.TimeoutError:
                    continue

    # ---- mailbox --------------------------------------------------------

    async def send_mail(
        self,
        sender: Any,
        recipient: Any,
        body: Any,
        subject: Any = None,
        reply_to: Any = None,
        client_request_id: Any = None,
        message_id: Any = None,
    ) -> dict[str, Any]:
        """Send a direct mail. Any valid handle may receive it, the sender included.

        ``reply_to`` must be a mail the sender sent or received. ``message_id``
        optionally links the mail to the chat message that prompted it.
        ``client_request_id`` makes a retry return the original mail. The
        ``mail`` event carries ids, sender and subject, never the body.
        """
        clean_sender = mailbox.sender(sender)
        clean_recipient = mailbox.recipient(recipient)
        clean_body = mailbox.body(body)
        clean_subject = mailbox.subject(subject)
        parent_id = mailbox.reply_to(reply_to)
        linked_message = mailbox.message_id(message_id)
        # The parameter shadows validate.client_request_id here.
        key = validate.client_request_id(client_request_id)
        payload = {
            "sender": clean_sender,
            "recipient": clean_recipient,
            "subject": clean_subject,
            "body": clean_body,
            "reply_to": parent_id,
        }
        # Omitted when unset so retries of mail stored before message_id existed still match.
        if linked_message is not None:
            payload["message_id"] = linked_message
        digest = fingerprint("mail", payload) if key else None

        async def op(conn):
            if key and digest is not None:
                prior = await store.get_idempotency(conn, key)
                if prior is not None:
                    if prior["fingerprint"] != digest or prior["kind"] != "mail":
                        raise HubError(
                            "idempotency_conflict",
                            "client_request_id was already used with a different payload",
                            409,
                            {"client_request_id": key},
                        )
                    return prior["result"]
            if parent_id is not None:
                parent = await store.get_mail(conn, parent_id)
                if parent is None or clean_sender not in (parent["sender"], parent["recipient"]):
                    raise HubError(
                        "validation_error",
                        "reply_to must be a mail you sent or received",
                        400,
                        {"field": "reply_to"},
                    )
            if linked_message is not None and await store.get_message(conn, linked_message) is None:
                raise HubError(
                    "validation_error",
                    "message_id must be an existing chat message",
                    400,
                    {"field": "message_id"},
                )
            created_at = utc_now()
            mail_id = await store.insert_mail(
                conn,
                recipient=clean_recipient,
                sender=clean_sender,
                subject=clean_subject,
                body=clean_body,
                reply_to=parent_id,
                created_at=created_at,
                message_id=linked_message,
            )
            mail = await store.get_mail(conn, mail_id)
            assert mail is not None
            await store.insert_event(
                conn, "mail", None, mail_id, mailbox.event_payload(mail), created_at
            )
            result = {"mail": mail}
            if key and digest is not None:
                await store.insert_idempotency(conn, key, "mail", digest, result, created_at)
            return result

        return await self._txn(op)

    async def list_mail(
        self,
        agent: Any,
        *,
        state: Any = "unread",
        since_id: Any = 0,
        limit: Any = None,
    ) -> dict[str, Any]:
        """``agent``'s mail after ``since_id``, oldest first. Page with the returned ``cursor``."""
        clean_agent = handle(agent, "agent")
        clean_state = mailbox.state(state)
        since = optional_id(since_id, "since_id") or 0
        clean_limit = page_limit(limit)

        async def op(conn):
            return await store.list_mail(
                conn, clean_agent, state=clean_state, since_id=since, limit=clean_limit
            )

        found, has_more = await self.db.read(op)
        return {
            "mail": found,
            "cursor": found[-1]["id"] if found else since,
            "has_more": has_more,
            "agent": clean_agent,
            "state": clean_state,
        }

    async def mail_counts(self, agent: Any) -> dict[str, Any]:
        clean_agent = handle(agent, "agent")

        async def op(conn):
            return await store.mail_counts(conn, clean_agent)

        return {"agent": clean_agent, **await self.db.read(op)}

    @staticmethod
    async def _require_own_mail(conn, agent: str, ids: list[int]) -> None:
        """Every id must exist and be addressed to ``agent``."""
        owners = await store.get_mail_owners(conn, ids)
        missing = [mail_id for mail_id in ids if mail_id not in owners]
        if missing:
            raise HubError("not_found", "mail not found", 404, {"ids": missing})
        foreign = [mail_id for mail_id in ids if owners[mail_id] != agent]
        if foreign:
            raise HubError(
                "forbidden", "only the recipient may read or ack a mail", 403, {"ids": foreign}
            )

    async def mark_mail_read(
        self, agent: Any, ids: Any = None, up_to_id: Any = None
    ) -> dict[str, Any]:
        """Mark ``ids``, or all of ``agent``'s mail up to ``up_to_id``, read. Repeating is a no-op."""
        clean_agent = handle(agent, "agent")
        clean_ids = mailbox.ids(ids) if ids not in (None, "", []) else None
        up_to = optional_id(up_to_id, "up_to_id")
        if (clean_ids is None) == (up_to is None):
            raise HubError(
                "validation_error", "send exactly one of ids or up_to_id", 400, {"field": "ids"}
            )

        async def op(conn):
            if clean_ids is not None:
                await self._require_own_mail(conn, clean_agent, clean_ids)
            changed = await store.mark_mail_read(
                conn, clean_agent, mail_ids=clean_ids, up_to_id=up_to, read_at=utc_now()
            )
            counts = await store.mail_counts(conn, clean_agent)
            return {"agent": clean_agent, "updated": changed, **counts}

        return await self._txn(op)

    async def ack_mail(self, agent: Any, ids: Any = None) -> dict[str, Any]:
        """Acknowledge ``ids``. Ack implies read. Repeating is a no-op."""
        clean_agent = handle(agent, "agent")
        clean_ids = mailbox.ids(ids)

        async def op(conn):
            await self._require_own_mail(conn, clean_agent, clean_ids)
            changed = await store.ack_mail(conn, clean_agent, clean_ids, utc_now())
            counts = await store.mail_counts(conn, clean_agent)
            return {"agent": clean_agent, "updated": changed, **counts}

        return await self._txn(op)

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
            await self._index(conn, "task", task_id)
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
            await self._index(conn, "task", clean_id)
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
            await self._index(conn, "task", clean_id)
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

    async def search(
        self,
        q: Any,
        *,
        kind: Any = None,
        channel: Any = None,
        author: Any = None,
        since: Any = None,
        limit: Any = None,
        offset: Any = None,
        mode: Any = None,
    ) -> dict[str, Any]:
        """Search over messages, tasks, attachment filenames and active knowledge facts.

        ``bm25`` (default) is full-text search. ``vector`` ranks by cosine
        similarity to the embedded query, ``hybrid`` fuses both with
        reciprocal rank fusion; both need an embedder and otherwise answer
        ``not_available``. Archived channels are searched too. A query with
        no searchable words returns no results rather than an error.
        """
        clean_mode = search_rules.search_mode(mode, self._search_modes())
        text = search_rules.query_text(q)
        kinds = search_rules.search_kinds(kind)
        clean_channel = channel_name(channel) if channel not in (None, "") else None
        clean_author = handle(author, "author") if author not in (None, "") else None
        clean_since = search_rules.search_since(since)
        clean_limit = search_rules.search_limit(limit)
        clean_offset = search_rules.search_offset(offset)
        if not self.db.fts5_available:
            raise HubError(
                "search_unavailable",
                "full-text search needs SQLite with FTS5, which this build lacks",
                501,
            )
        match = search_rules.build_match(text)

        async def op(conn, limit=clean_limit, offset=clean_offset):
            channel_id = None
            if clean_channel is not None:
                found = await store.get_channel_by_name(conn, clean_channel)
                if found is None:
                    raise HubError("not_found", f"channel {clean_channel} not found", 404)
                channel_id = found["id"]
            if match is None:
                return [], 0
            return await store.search_index(
                conn,
                match,
                kinds=kinds,
                channel_id=channel_id,
                author=clean_author,
                since=clean_since,
                limit=limit,
                offset=offset,
                weights=search_rules.BM25_WEIGHTS,
                snippet_markers=search_rules.SNIPPET_MARKERS,
                snippet_tokens=search_rules.SNIPPET_TOKENS,
            )

        if clean_mode == "bm25":
            rows, total = await self.db.read(op)
            return search_rules.shape_results(
                rows, total, limit=clean_limit, offset=clean_offset, mode=clean_mode
            )

        filters = {"kinds": kinds, "channel": clean_channel, "author": clean_author, "since": clean_since}
        if clean_mode == "vector":
            if not text.strip():
                return search_rules.empty_results(limit=clean_limit, offset=clean_offset, mode="vector")
            query_vec = await self._embed_query(text, degrade=False)
            scored, total = await self._vector_rank(query_vec, clean_offset + clean_limit, filters)
            page = scored[clean_offset : clean_offset + clean_limit]
            hits = [search_rules.shape_vector_hit(row, value) for value, row in page]
            return search_rules.shape_scored(
                hits, total, limit=clean_limit, offset=clean_offset, mode="vector"
            )

        # hybrid
        query_vec = await self._embed_query(text, degrade=True) if text.strip() else None
        if query_vec is None and text.strip():
            rows, total = await self.db.read(op)
            result = search_rules.shape_results(
                rows, total, limit=clean_limit, offset=clean_offset, mode="hybrid"
            )
            result["degraded"] = "bm25"
            return result
        pool = search_rules.fusion_pool(clean_limit)
        bm25_rows, _ = await self.db.read(lambda conn: op(conn, limit=pool, offset=0))
        vector_scored: list[tuple[float, dict[str, Any]]] = []
        if query_vec is not None:
            vector_scored, _ = await self._vector_rank(query_vec, pool, filters)
        by_key: dict[tuple[str, int], dict[str, Any]] = {}
        for _, row in vector_scored:
            by_key[(row["kind"], int(row["ref_id"]))] = row
        for row in bm25_rows:
            # BM25 rows win: their snippet carries the match highlight.
            by_key[(row["kind"], int(row["ref_id"]))] = row
        fused = search_rules.rrf_fuse(
            [
                [(row["kind"], int(row["ref_id"])) for row in bm25_rows],
                [(row["kind"], int(row["ref_id"])) for _, row in vector_scored],
            ]
        )
        page = fused[clean_offset : clean_offset + clean_limit]
        hits = [search_rules.shape_vector_hit(by_key[key], value) for key, value in page]
        # Paging stops at the end of the fused pool, so it never points at empty pages.
        total = len(fused)
        return search_rules.shape_scored(
            hits, total, limit=clean_limit, offset=clean_offset, mode="hybrid"
        )

    def _search_modes(self) -> frozenset[str]:
        if self.embedder.enabled:
            return frozenset(search_rules.SEARCH_MODES)
        return search_rules.AVAILABLE_MODES

    async def _embed_query(self, text: str, *, degrade: bool) -> list[float] | None:
        """The unit query vector. On failure: ``None`` when ``degrade``, else ``embedder_unavailable``."""
        timeout = min(self.embedder.timeout or search_rules.QUERY_EMBED_TIMEOUT, search_rules.QUERY_EMBED_TIMEOUT)
        try:
            vectors = await asyncio.wait_for(self.embedder.embed([text[: embed.EMBED_TEXT_MAX]]), timeout)
            if len(vectors) != 1 or not vectors[0]:
                raise embed.EmbedderError("embedder returned no vector for the query")
            return embed.normalize(vectors[0])
        except Exception as exc:  # noqa: BLE001 — any provider failure maps to one clear error
            reason = embed.safe_error(exc)
            logger.warning("query embedding failed (%s)", reason)
            if degrade:
                return None
            raise HubError(
                "embedder_unavailable",
                f"the embedder could not embed the query ({reason}); try mode=bm25",
                503,
                {"provider": self.embedder.provider, "model": self.embedder.name, "reason": reason},
            ) from None

    async def _vector_rank(
        self, query_vec: list[float], k: int, filters: dict[str, Any]
    ) -> tuple[list[tuple[float, dict[str, Any]]], int]:
        """The ``k`` entries most similar to ``query_vec`` that pass ``filters``, plus how many were scored.

        At most :data:`search.VECTOR_SCAN_MAX` of the newest entries are scored.
        """
        model = self.embedder.name
        dim = len(query_vec)

        async def op(conn):
            channel_id = None
            if filters["channel"] is not None:
                found = await store.get_channel_by_name(conn, filters["channel"])
                if found is None:
                    raise HubError("not_found", f"channel {filters['channel']} not found", 404)
                channel_id = found["id"]
            count = 0
            scored = []
            async for row in store.vector_candidates(
                conn,
                model,
                dim,
                kinds=filters["kinds"],
                channel_id=channel_id,
                author=filters["author"],
                since=filters["since"],
                scan_limit=search_rules.VECTOR_SCAN_MAX,
            ):
                count += 1
                scored.append((embed.dot(query_vec, embed.unpack(row["vector"])), row))
                if len(scored) >= 4 * k + 256:
                    scored = search_rules.top_k(scored, k)
            best = search_rules.top_k(scored, k)
            out = [(value, dict(row)) for value, row in best]
            await store.attach_knowledge_meta(conn, [row for _, row in out])
            return out, count

        return await self.db.read(op)

    async def embed_status(self) -> dict[str, Any]:
        """Embedder and queue state: ``{provider, model, dim, indexed, pending, last_error}``."""
        embedder = self.embedder
        queue = self.embed_queue
        counts = {"indexed": 0, "pending": 0}
        if embedder.enabled and self.db.fts5_available:
            counts = await self.db.read(
                lambda conn: store.embed_counts(conn, embedder.name, embedder.dim)
            )
        return {
            "provider": embedder.provider,
            "model": embedder.name or None,
            "dim": embedder.dim,
            "indexed": counts["indexed"],
            "pending": counts["pending"],
            "last_error": queue.last_error if queue is not None else None,
        }

    async def get_attachment(self, attachment_id: Any) -> dict[str, Any]:
        clean_id = require_id(attachment_id, "attachment_id")

        async def op(conn):
            return await store.get_attachment(conn, clean_id)

        found = await self.db.read(op)
        if found is None:
            raise HubError("not_found", f"attachment {clean_id} not found", 404)
        return {"attachment": found}

    # ---- feed -----------------------------------------------------------

    async def _shape_feed(self, conn, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """API shapes for feed rows, with counts, hashtags and the one-level inline original."""
        if not rows:
            return []
        targets = {
            target
            for row in rows
            if (target := row.get("repost_of") or row.get("quote_of")) is not None
        }
        originals = await store.feed_rows(conn, targets)
        ids = [row["id"] for row in rows] + list(originals)
        counts = await store.feed_counts(conn, ids)
        tags = await store.feed_tags_for(conn, ids)
        reacted = await store.reaction_counts(conn, ids)
        inline: dict[int, dict[str, Any]] = {}
        for target in targets:
            found = originals.get(target)
            if found is None or found["deleted_at"] is not None:
                inline[target] = feed.tombstone_original(target)
            else:
                inline[target] = feed.shape_post(
                    found,
                    counts=counts.get(target),
                    hashtags=tags.get(target),
                    reactions=reacted.get(target),
                )
        return [
            feed.shape_post(
                row,
                counts=counts.get(row["id"]),
                hashtags=tags.get(row["id"]),
                original=inline.get(row.get("repost_of") or row.get("quote_of")),
                reactions=reacted.get(row["id"]),
            )
            for row in rows
        ]

    async def _feed_post_shape(self, conn, post_id: int) -> dict[str, Any]:
        row = await store.get_feed_row(conn, post_id)
        assert row is not None
        return (await self._shape_feed(conn, [row]))[0]

    @staticmethod
    async def _live_original(conn, post_id: int, field: str) -> dict[str, Any]:
        """The post a repost or quote points at. A repost resolves to what it reposted."""
        found = await store.get_feed_row(conn, post_id)
        if found is not None and found["repost_of"] is not None:
            found = await store.get_feed_row(conn, found["repost_of"])
        if found is None:
            raise HubError("not_found", f"post {post_id} not found", 404, {"field": field})
        if found["deleted_at"] is not None:
            raise HubError("post_deleted", f"post {found['id']} was deleted", 409, {"field": field})
        return found

    async def feed_post(
        self,
        author: Any,
        body: Any,
        parent_id: Any = None,
        quote_of: Any = None,
        client_request_id: Any = None,
    ) -> dict[str, Any]:
        """Post to the feed. ``parent_id`` makes it a reply, ``quote_of`` a quote.

        A reply inherits ``root_id`` from its parent and sits one level deeper,
        at most ``feed.DEPTH_MAX``. The parent must exist and not be deleted.
        ``client_request_id`` makes a retry return the first result.

        After the commit, every callable in ``self.feed_post_hooks`` is awaited
        with the result, for new posts only (not idempotent replays). This is
        the extension point for later features (knowledge facts); a failing
        hook is logged and never undoes or fails the post.
        """
        clean_author = handle(author, "author")
        clean_body = feed.normalize_body(body)
        parent = optional_id(parent_id, "parent_id") or None
        quoted = optional_id(quote_of, "quote_of") or None
        key = client_request_id_fn(client_request_id)
        payload = {"author": clean_author, "body": clean_body, "parent_id": parent, "quote_of": quoted}
        digest = fingerprint("feed_post", payload) if key else None
        tags = feed.extract_hashtags(clean_body)

        async def op(conn):
            if key and digest is not None:
                prior = await store.get_idempotency(conn, key)
                if prior is not None:
                    if prior["fingerprint"] != digest or prior["kind"] != "feed_post":
                        raise HubError(
                            "idempotency_conflict",
                            "client_request_id was already used with a different payload",
                            409,
                            {"client_request_id": key},
                        )
                    return prior["result"], False
            root_id: int | None = None
            depth = 0
            if parent is not None:
                found = await store.get_feed_row(conn, parent)
                if found is None:
                    raise HubError("not_found", f"post {parent} not found", 404, {"field": "parent_id"})
                if found["deleted_at"] is not None:
                    raise HubError(
                        "post_deleted", f"post {parent} was deleted", 409, {"field": "parent_id"}
                    )
                depth = int(found["depth"]) + 1
                if depth > feed.DEPTH_MAX:
                    raise HubError(
                        "thread_too_deep",
                        f"replies nest at most {feed.DEPTH_MAX} levels deep",
                        409,
                        {"field": "parent_id", "max_depth": feed.DEPTH_MAX},
                    )
                root_id = int(found["root_id"])
            quote_target = None
            if quoted is not None:
                quote_target = (await self._live_original(conn, quoted, "quote_of"))["id"]
            created_at = utc_now()
            post_id = await store.insert_feed_post(
                conn,
                author=clean_author,
                body=clean_body,
                parent_id=parent,
                root_id=root_id,
                repost_of=None,
                quote_of=quote_target,
                depth=depth,
                created_at=created_at,
                tags=tags,
            )
            post = await self._feed_post_shape(conn, post_id)
            await store.insert_event(
                conn,
                "feed_post",
                None,
                post_id,
                {
                    "id": post_id,
                    "kind": post["kind"],
                    "author": clean_author,
                    "parent_id": parent,
                    "root_id": post["root_id"],
                    "quote_of": quote_target,
                    "depth": depth,
                    "hashtags": tags,
                    "body": feed.event_snippet(clean_body),
                },
                created_at,
            )
            result = {"post": post}
            if key and digest is not None:
                await store.insert_idempotency(conn, key, "feed_post", digest, result, created_at)
            return result, True

        result, created = await self._txn(op)
        if created:
            for hook in list(self.feed_post_hooks):
                try:
                    await hook(result)
                except Exception:
                    logger.exception("feed post hook failed")
        return result

    async def feed_repost(self, author: Any, post_id: Any) -> dict[str, Any]:
        """Repost a post. Repeating it returns the same repost; a repost of a repost reposts the original."""
        clean_author = handle(author, "author")
        target = require_id(post_id, "post_id")

        async def op(conn):
            original = await self._live_original(conn, target, "post_id")
            existing = await store.find_repost(conn, clean_author, original["id"])
            if existing is not None and existing["deleted_at"] is None:
                return {"post": await self._feed_post_shape(conn, existing["id"]), "idempotent": True}
            created_at = utc_now()
            if existing is not None:
                # Reposting again after deleting the repost brings the same row back, restamped.
                repost_id = existing["id"]
                await store.revive_feed_post(conn, repost_id, created_at)
            else:
                repost_id = await store.insert_feed_post(
                    conn,
                    author=clean_author,
                    body="",
                    parent_id=None,
                    root_id=None,
                    repost_of=original["id"],
                    quote_of=None,
                    depth=0,
                    created_at=created_at,
                )
            await store.insert_event(
                conn,
                "feed_repost",
                None,
                repost_id,
                {
                    "id": repost_id,
                    "author": clean_author,
                    "repost_of": original["id"],
                    "original_author": original["author"],
                    "body": feed.event_snippet(original["body"]),
                },
                created_at,
            )
            return {"post": await self._feed_post_shape(conn, repost_id), "idempotent": False}

        return await self._txn(op)

    async def feed_delete(self, actor: Any, post_id: Any) -> dict[str, Any]:
        """Soft delete. Only the author may delete; replies stay and the post becomes a tombstone."""
        clean_actor = handle(actor, "actor")
        target = require_id(post_id, "post_id")

        async def op(conn):
            found = await store.get_feed_row(conn, target)
            if found is None:
                raise HubError("not_found", f"post {target} not found", 404)
            if found["author"] != clean_actor:
                raise HubError("forbidden", "only the author can delete a post", 403, {"post_id": target})
            if found["deleted_at"] is not None:
                return {"post": await self._feed_post_shape(conn, target), "idempotent": True}
            await store.set_feed_deleted(conn, target, utc_now())
            return {"post": await self._feed_post_shape(conn, target), "idempotent": False}

        return await self._txn(op)

    async def feed_get_post(self, post_id: Any) -> dict[str, Any]:
        """One post. A deleted post comes back as a tombstone."""
        target = require_id(post_id, "post_id")

        async def op(conn):
            found = await store.get_feed_row(conn, target)
            if found is None:
                raise HubError("not_found", f"post {target} not found", 404)
            return {"post": (await self._shape_feed(conn, [found]))[0]}

        return await self.db.read(op)

    async def _set_follow(self, follower: Any, followee: Any, following: bool) -> dict[str, Any]:
        clean_follower = handle(follower, "follower")
        clean_followee = handle(followee, "followee")
        if clean_follower == clean_followee:
            raise HubError("validation_error", "a handle cannot follow itself", 400, {"field": "followee"})

        async def op(conn):
            created_at = utc_now()
            if following:
                changed = await store.insert_follow(conn, clean_follower, clean_followee, created_at)
            else:
                changed = await store.delete_follow(conn, clean_follower, clean_followee)
            if changed:
                await store.insert_event(
                    conn,
                    "feed_follow",
                    None,
                    0,
                    {"follower": clean_follower, "followee": clean_followee, "following": following},
                    created_at,
                )
            return {
                "follower": clean_follower,
                "followee": clean_followee,
                "following": following,
                "idempotent": not changed,
            }

        return await self._txn(op)

    async def feed_follow(self, follower: Any, followee: Any) -> dict[str, Any]:
        """``follower`` follows ``followee``. Repeating it is a no-op."""
        return await self._set_follow(follower, followee, True)

    async def feed_unfollow(self, follower: Any, followee: Any) -> dict[str, Any]:
        """Stop following. Unfollowing someone you do not follow is a no-op."""
        return await self._set_follow(follower, followee, False)

    async def feed_following(self, handle_value: Any) -> dict[str, Any]:
        clean = handle(handle_value, "handle")

        async def op(conn):
            return await store.list_follows(conn, follower=clean)

        rows = await self.db.read(op)
        return {"handle": clean, "following": [row["followee"] for row in rows]}

    async def feed_followers(self, handle_value: Any) -> dict[str, Any]:
        clean = handle(handle_value, "handle")

        async def op(conn):
            return await store.list_follows(conn, followee=clean)

        rows = await self.db.read(op)
        return {"handle": clean, "followers": [row["follower"] for row in rows]}

    async def feed_timeline(
        self,
        *,
        scope: Any = "global",
        handle: Any = None,
        tag: Any = None,
        author: Any = None,
        post_id: Any = None,
        cursor: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Newest-first live posts with opaque ``cursor`` paging, or one thread.

        ``global`` is everything, ``following`` the authors ``handle`` follows,
        ``hashtag`` posts tagged ``tag``, ``author`` one author's posts. Deleted
        posts are left out. ``thread`` returns the nested tree around
        ``post_id`` (any post in it), with deleted posts as tombstones; see
        ``_feed_thread`` for its window and paging.
        """
        clean_scope = scope or "global"
        if clean_scope not in feed.TIMELINE_SCOPES:
            raise HubError(
                "validation_error",
                f"scope must be one of {', '.join(feed.TIMELINE_SCOPES)}",
                400,
                {"field": "scope", "allowed": list(feed.TIMELINE_SCOPES)},
            )
        if clean_scope == "thread":
            return await self._feed_thread(require_id(post_id, "post_id"), cursor)
        before = feed.decode_cursor(cursor)
        clean_limit = page_limit(limit)
        filters: dict[str, Any] = {}
        if clean_scope == "following":
            if handle in (None, ""):
                raise HubError("validation_error", "following needs handle", 400, {"field": "handle"})
            filters["follower"] = validate_handle(handle, "handle")
        elif clean_scope == "hashtag":
            filters["tag"] = feed.clean_tag(tag)
        elif clean_scope == "author":
            who = author if author not in (None, "") else handle
            if who in (None, ""):
                raise HubError(
                    "validation_error", "author scope needs author or handle", 400, {"field": "author"}
                )
            filters["author"] = validate_handle(who, "author")

        async def op(conn):
            rows = await store.feed_timeline_rows(
                conn, before_id=before, limit=clean_limit + 1, **filters
            )
            has_more = len(rows) > clean_limit
            rows = rows[:clean_limit]
            return await self._shape_feed(conn, rows), has_more

        posts, has_more = await self.db.read(op)
        return {
            "scope": clean_scope,
            "posts": posts,
            "next_cursor": feed.encode_cursor(posts[-1]["id"]) if has_more and posts else None,
            "has_more": has_more,
        }

    async def _feed_thread(self, post_id: int, cursor: Any = None) -> dict[str, Any]:
        """A window of ``feed.THREAD_NEST_MAX`` levels of the thread around ``post_id``.

        The window starts at the root, or a few levels above a deep post; the
        posts above it come back flat in ``ancestors``. Nodes on the window's
        last level with replies carry ``more_replies`` (read the thread at that
        node to go deeper). A wide window pages by ``next_cursor``; the
        requested post and its chain are always on the first page.
        """
        after = feed.decode_cursor(cursor)

        async def op(conn):
            chain = await store.feed_ancestor_rows(conn, post_id)
            if not chain:
                raise HubError("not_found", f"post {post_id} not found", 404)
            focus_depth = int(chain[-1]["depth"])
            top_depth = feed.thread_window_top(focus_depth)
            top = chain[top_depth]
            max_depth = top_depth + feed.THREAD_NEST_MAX - 1
            in_window = chain[top_depth:]
            rows = await store.feed_subtree_rows(
                conn,
                int(top["id"]),
                max_depth=max_depth,
                after_id=after,
                limit=feed.THREAD_ROWS_MAX + 1,
                exclude=[row["id"] for row in in_window] if after is not None else (),
            )
            has_more = len(rows) > feed.THREAD_ROWS_MAX
            rows = rows[: feed.THREAD_ROWS_MAX]
            last_id = rows[-1]["id"] if rows else None
            if after is None:
                listed = {row["id"] for row in rows}
                rows += [row for row in in_window if row["id"] not in listed]
            edge = [row["id"] for row in rows if row["depth"] >= max_depth]
            deeper = await store.feed_ids_with_replies(conn, edge)
            posts = await self._shape_feed(conn, rows)
            for post in posts:
                post["more_replies"] = post["id"] in deeper
            ancestors = await self._shape_feed(conn, chain[:top_depth])
            return int(chain[0]["id"]), int(top["id"]), ancestors, posts, has_more, last_id

        root_id, top_id, ancestors, posts, has_more, last_id = await self.db.read(op)
        return {
            "scope": "thread",
            "root_id": root_id,
            "focus_id": post_id,
            "top_id": top_id,
            "ancestors": ancestors,
            "count": len(posts),
            "posts": feed.build_thread(posts, focus_id=post_id),
            "next_cursor": feed.encode_cursor(last_id) if has_more and last_id else None,
            "has_more": has_more,
            "truncated": has_more,
        }

    async def feed_trending_tags(self, limit: Any = None, since: Any = None) -> dict[str, Any]:
        """Most used tags on live posts since ``since`` (ISO time, default the last 24 hours)."""
        if limit in (None, ""):
            clean_limit = feed.TRENDING_DEFAULT
        else:
            clean_limit = min(page_limit(limit), feed.TRENDING_MAX)
        if since in (None, ""):
            start = datetime.now(timezone.utc) - timedelta(hours=feed.TRENDING_WINDOW_HOURS)
        else:
            try:
                start = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            except ValueError as exc:
                raise HubError(
                    "validation_error", "since must be an ISO 8601 time", 400, {"field": "since"}
                ) from exc
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        clean_since = start.astimezone(timezone.utc).isoformat()

        async def op(conn):
            return await store.trending_tags(conn, since=clean_since, limit=clean_limit)

        return {"since": clean_since, "tags": await self.db.read(op)}

    # ---- reactions ------------------------------------------------------

    @staticmethod
    async def _reaction_target(conn, post_id: int, *, live: bool) -> dict[str, Any]:
        """The post a reaction lands on. A plain repost resolves to what it reposted.

        ``live`` also refuses a deleted post, with the same not-found error as a missing one.
        """
        found = await store.get_feed_row(conn, post_id)
        if found is not None and found["repost_of"] is not None:
            found = await store.get_feed_row(conn, found["repost_of"])
        if found is None or (live and found["deleted_at"] is not None):
            raise HubError("not_found", f"post {post_id} not found", 404, {"field": "post_id"})
        return found

    @staticmethod
    async def _reaction_state(conn, post_id: int, who: str) -> dict[str, Any]:
        counts = (await store.reaction_counts(conn, [post_id])).get(post_id, {})
        return {
            "post_id": post_id,
            "handle": who,
            "reactions": counts,
            "score": feed.reaction_score(counts),
            "mine": await store.handle_reactions(conn, post_id, who),
        }

    @staticmethod
    async def _reaction_event(
        conn, kind: str, post: dict[str, Any], who: str, reaction: str, created_at: str
    ) -> None:
        await store.insert_event(
            conn,
            kind,
            None,
            int(post["id"]),
            {"post_id": int(post["id"]), "handle": who, "kind": reaction, "post_author": post["author"]},
            created_at,
        )

    async def react(self, handle_value: Any, post_id: Any, kind: Any) -> dict[str, Any]:
        """``handle`` reacts to a post with ``kind`` (``feed.REACTION_KINDS``).

        Repeating a reaction is a no-op. ``+1`` and ``-1`` exclude each other:
        voting one removes the other (``replaced`` names it). Emoji kinds are
        independent. A missing or deleted post is not found.
        """
        who = handle(handle_value, "handle")
        target = require_id(post_id, "post_id")
        clean_kind = feed.reaction_kind(kind)

        async def op(conn):
            post = await self._reaction_target(conn, target, live=True)
            created_at = utc_now()
            replaced: list[str] = []
            if clean_kind in feed.VOTE_KINDS:
                for other in feed.VOTE_KINDS:
                    if other != clean_kind and await store.delete_reaction(conn, post["id"], who, other):
                        replaced.append(other)
                        await self._reaction_event(conn, "reaction_removed", post, who, other, created_at)
            added = await store.insert_reaction(conn, post["id"], who, clean_kind, created_at)
            if added:
                await self._reaction_event(conn, "reaction_added", post, who, clean_kind, created_at)
            state = await self._reaction_state(conn, int(post["id"]), who)
            return {**state, "kind": clean_kind, "replaced": replaced, "idempotent": not added}

        return await self._txn(op)

    async def unreact(self, handle_value: Any, post_id: Any, kind: Any) -> dict[str, Any]:
        """Remove one of ``handle``'s reactions. Removing one it does not hold is a no-op."""
        who = handle(handle_value, "handle")
        target = require_id(post_id, "post_id")
        clean_kind = feed.reaction_kind(kind)

        async def op(conn):
            post = await self._reaction_target(conn, target, live=False)
            removed = await store.delete_reaction(conn, post["id"], who, clean_kind)
            if removed:
                await self._reaction_event(conn, "reaction_removed", post, who, clean_kind, utc_now())
            state = await self._reaction_state(conn, int(post["id"]), who)
            return {**state, "kind": clean_kind, "idempotent": not removed}

        return await self._txn(op)

    async def list_reactions(self, post_id: Any) -> dict[str, Any]:
        """Counts and reacting handles per kind for one post, plus ``score``."""
        target = require_id(post_id, "post_id")

        async def op(conn):
            post = await self._reaction_target(conn, target, live=False)
            return int(post["id"]), await store.reaction_rows(conn, int(post["id"]))

        found_id, rows = await self.db.read(op)
        by_kind: dict[str, list[str]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(row["handle"])
        counts = {kind: len(by_kind[kind]) for kind in feed.REACTION_KINDS if kind in by_kind}
        return {
            "post_id": found_id,
            "reactions": counts,
            "handles": {kind: by_kind[kind] for kind in counts},
            "score": feed.reaction_score(counts),
        }

    async def reaction_counts(self, post_ids: Any) -> dict[int, dict[str, Any]]:
        """``{post_id: {"reactions": {kind: n}, "score": n}}`` for many posts in one query.

        Every requested id is in the answer, with empty counts when nothing reacted.
        """
        ids = sorted({require_id(value, "post_ids") for value in (post_ids or [])})

        async def op(conn):
            return await store.reaction_counts(conn, ids)

        found = await self.db.read(op) if ids else {}
        return {
            post_id: {"reactions": found.get(post_id, {}), "score": feed.reaction_score(found.get(post_id, {}))}
            for post_id in ids
        }

    # ---- webhooks -------------------------------------------------------

    # ---- knowledge facts --------------------------------------------------

    async def _knowledge_insert(self, conn, clean: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Insert one validated fact, or return the existing one with the same dedupe key.

        Runs inside the caller's write transaction. A new fact is indexed for
        search and announced with a ``knowledge_fact`` event.
        """
        norm = knowledge.normalize_fact(clean["fact"])
        existing = await store.find_knowledge(conn, clean["project"], norm)
        if existing is not None:
            return knowledge.shape_fact(existing), False
        now = utc_now()
        fact_id = await store.insert_knowledge(
            conn,
            {
                "project": clean["project"],
                "topic": clean["topic"],
                "fact": clean["fact"],
                "norm": norm,
                "source_run": clean.get("source_run"),
                "source_step": clean.get("source_step"),
                "author": clean["author"],
                "status": "active",
                "created_at": clean.get("created_at") or now,
                "updated_at": now,
            },
        )
        await self._index(conn, "knowledge", fact_id)
        row = await store.get_knowledge(conn, fact_id)
        assert row is not None
        fact = knowledge.shape_fact(row)
        await store.insert_event(
            conn, "knowledge_fact", None, fact_id, knowledge.event_payload(fact), now
        )
        return fact, True

    async def knowledge_add(
        self,
        project: Any,
        fact: Any,
        topic: Any = None,
        author: Any = None,
        source_run: Any = None,
        source_step: Any = None,
    ) -> dict[str, Any]:
        """Record a fact. The same fact again in ``project`` (any case, spacing or
        trailing punctuation) returns the existing row with ``created: false``.
        """
        clean = {
            "project": knowledge.project_slug(project),
            "topic": knowledge.topic_slug(topic),
            "fact": knowledge.fact_text(fact),
            "author": handle(author, "author"),
            "source_run": knowledge.source_id(source_run, "source_run"),
            "source_step": knowledge.source_id(source_step, "source_step"),
        }

        async def op(conn):
            return await self._knowledge_insert(conn, clean)

        found, created = await self._txn(op)
        return {"fact": found, "created": created}

    async def knowledge_list(
        self,
        project: Any = None,
        topic: Any = None,
        status: Any = None,
        author: Any = None,
        cursor: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Facts newest first with opaque ``cursor`` paging. Filters are exact matches."""
        filters = {
            "project": knowledge.optional_project(project),
            "topic": knowledge.optional_topic(topic),
            "status": knowledge.optional_status(status),
            "author": optional_handle(author, "author"),
        }
        before = feed.decode_cursor(cursor)
        clean_limit = page_limit(limit)

        async def op(conn):
            return await store.list_knowledge(conn, before_id=before, limit=clean_limit + 1, **filters)

        rows = await self.db.read(op)
        has_more = len(rows) > clean_limit
        facts = [knowledge.shape_fact(row) for row in rows[:clean_limit]]
        return {
            "facts": facts,
            "next_cursor": feed.encode_cursor(facts[-1]["id"]) if has_more and facts else None,
            "has_more": has_more,
        }

    async def knowledge_set_status(self, actor: Any, fact_id: Any, status: Any) -> dict[str, Any]:
        """Mark a fact ``active``, ``superseded`` or ``rejected``. Only active facts are searchable."""
        clean_actor = handle(actor, "actor")
        clean_id = require_id(fact_id, "fact_id")
        target = knowledge.status_value(status)

        async def op(conn):
            found = await store.get_knowledge(conn, clean_id)
            if found is None:
                raise HubError("not_found", f"fact {clean_id} not found", 404)
            previous = found["status"]
            if previous == target:
                return {"fact": knowledge.shape_fact(found), "previous_status": previous, "idempotent": True}
            await store.set_knowledge_status(conn, clean_id, target, utc_now())
            await self._index(conn, "knowledge", clean_id)
            fresh = await store.get_knowledge(conn, clean_id)
            assert fresh is not None
            logger.info("fact %s: %s -> %s by %s", clean_id, previous, target, clean_actor)
            return {"fact": knowledge.shape_fact(fresh), "previous_status": previous, "idempotent": False}

        return await self._txn(op)

    async def knowledge_topics(self, project: Any = None) -> dict[str, Any]:
        """Active fact counts per topic, most facts first."""
        clean_project = knowledge.optional_project(project)

        async def op(conn):
            return await store.knowledge_topics(conn, project=clean_project, status="active")

        return {"project": clean_project, "topics": await self.db.read(op)}

    async def knowledge_import_jsonl(self, lines: Any, author: Any) -> dict[str, Any]:
        """Import JSONL lines in the ``_knowledge-inbox.jsonl`` shape, in one transaction.

        A line's own ``author`` wins over ``author``. Blank lines are skipped;
        a bad line is reported in ``invalid`` (1-based ``line``) and the rest
        still import. Duplicates, of stored facts or of earlier lines, count
        in ``duplicates``. At most ``knowledge.IMPORT_MAX`` lines per call.
        """
        importer = handle(author, "author")
        items = knowledge.import_lines(lines)
        parsed: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        for number, line in enumerate(items, start=1):
            if isinstance(line, str) and not line.strip():
                continue
            try:
                clean = knowledge.parse_line(line)
            except HubError as exc:
                invalid.append({"line": number, "error": exc.message})
                continue
            clean["author"] = clean["author"] or importer
            parsed.append(clean)

        now = utc_now()
        seen_keys: set[tuple[str, str]] = set()
        candidates: list[dict[str, Any]] = []
        for clean in parsed:
            norm = knowledge.normalize_fact(clean["fact"])
            key = (clean["project"], norm)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(
                {
                    "project": clean["project"],
                    "topic": clean["topic"],
                    "fact": clean["fact"],
                    "norm": norm,
                    "source_run": clean.get("source_run"),
                    "source_step": clean.get("source_step"),
                    "author": clean["author"],
                    "status": "active",
                    "created_at": clean.get("created_at") or now,
                    "updated_at": now,
                }
            )

        async def op(conn):
            inserted = await store.insert_knowledge_batch(conn, candidates)
            if inserted:
                fact_ids = [int(row["id"]) for row in inserted]
                await self._index_knowledge_batch(conn, fact_ids)
                await store.insert_events(
                    conn,
                    [
                        {
                            "kind": "knowledge_fact",
                            "channel_id": None,
                            "entity_id": int(row["id"]),
                            "payload": knowledge.event_payload(knowledge.shape_fact(row)),
                            "created_at": now,
                        }
                        for row in inserted
                    ],
                )
            return len(inserted)

        added = await self._txn(op) if candidates else 0
        duplicates = len(parsed) - added
        return {"added": added, "duplicates": duplicates, "invalid": invalid}

    async def knowledge_export_jsonl(self, project: Any = None, status: Any = "active") -> dict[str, Any]:
        """Facts as JSONL in the inbox shape, oldest first. ``status`` ``all`` exports every status."""
        clean_project = knowledge.optional_project(project)
        clean_status = None if status == "all" else knowledge.status_value(status or "active")

        async def op(conn):
            return await store.export_knowledge(conn, project=clean_project, status=clean_status)

        rows = await self.db.read(op)
        lines = [knowledge.serialize_fact(knowledge.shape_fact(row)) for row in rows]
        return {"jsonl": "".join(line + "\n" for line in lines), "count": len(lines)}

    async def knowledge_export_chunks(self, project: Any = None, status: Any = "active"):
        """Validate export filters and return an async iterator of JSONL chunks (oldest first)."""
        clean_project = knowledge.optional_project(project)
        clean_status = None if status == "all" else knowledge.status_value(status or "active")

        async def generate():
            after_id = 0
            while True:
                batch_size = store.EXPORT_BATCH
                rows = await self.db.read(
                    lambda conn, cur=after_id, sz=batch_size: store.export_knowledge_page(
                        conn, project=clean_project, status=clean_status, after_id=cur, limit=sz
                    )
                )
                if not rows:
                    break
                after_id = int(rows[-1]["id"])
                yield "".join(
                    knowledge.serialize_fact(knowledge.shape_fact(row)) + "\n" for row in rows
                )
                if len(rows) < batch_size:
                    break

        return generate()

    async def knowledge_from_feed_post(self, result: dict[str, Any]) -> None:
        """``feed_post_hooks`` callback: a ``LEARNED:`` post becomes a fact by its author.

        The app registers it at startup. ``feed_post`` logs and swallows hook
        failures, so a bad fact never fails the post.
        """
        post = result.get("post") or {}
        found = knowledge.learned_from_post(post)
        if found is None:
            return
        try:
            await self.knowledge_add(
                found["project"],
                found["fact"],
                topic=found["topic"],
                author=post.get("author"),
                source_run=found["source_run"],
                source_step=found["source_step"],
            )
        except HubError as exc:
            logger.warning(
                "rejected LEARNED fact from feed post %s by %s: %s",
                post.get("id"),
                post.get("author"),
                exc.message,
            )
            result["knowledge_error"] = exc.message

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
        With an ``agent``, ``mail`` holds the full mail behind its ``mail``
        events and ``mail_unread`` the number still unread.
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

        async def page(found: list[dict[str, Any]], at: int, more: bool, timed_out: bool):
            result: dict[str, Any] = {
                "events": found,
                "cursor": at,
                "has_more": more,
                "timed_out": timed_out,
                "agent": clean_agent,
                # The full mail behind each ``mail`` event, for its recipient only.
                "mail": [],
            }
            if clean_agent is not None:
                result.update(await self._mail_for_events(clean_agent, found))
            return result

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
                    return await page(found, cursor, more, False)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return await page([], cursor, False, seconds > 0)
                try:
                    await asyncio.wait_for(self.waiters.condition.wait(), remaining)
                except asyncio.TimeoutError:
                    continue

    async def _mail_for_events(self, agent: str, found: list[dict[str, Any]]) -> dict[str, Any]:
        ids = [
            int(event["entity_id"])
            for event in found
            if event["kind"] == "mail" and (event.get("payload") or {}).get("recipient") == agent
        ]

        async def op(conn):
            rows = await store.get_mail_many(conn, ids)
            return [row for row in rows if row["recipient"] == agent], await store.mail_counts(
                conn, agent
            )

        rows, counts = await self.db.read(op)
        return {"mail": rows, "mail_unread": counts["unread"]}

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
