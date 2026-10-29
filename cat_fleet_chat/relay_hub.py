"""Business rules for the relay control plane: durable consumers and the audit log.

A mixin of :class:`cat_fleet_chat.hub.Hub`, kept in its own module so the
core hub stays readable. Same layering as the rest of the hub: clean every
argument first, then run one transaction (``_txn`` wakes waiters, ``_quiet``
does not), and record ``consumer.*`` events plus an audit row for every
control-plane change.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from cat_fleet_chat import consumers, store
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.validate import handle, optional_id, page_limit

SCAN_BATCH = 200
# One wait scans at most this many events before it returns, so a narrow
# consumer behind a long backlog cannot pin a read connection.
SCAN_MAX = 2_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_ts(value: Any, field: str) -> str:
    """An ISO-8601 timestamp, normalized to UTC like ``created_at``."""
    if not isinstance(value, str) or not value.strip():
        raise HubError("validation_error", f"{field} must be an ISO-8601 timestamp", 400, {"field": field})
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HubError(
            "validation_error", f"{field} must be an ISO-8601 timestamp", 400, {"field": field}
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def public_consumer(consumer: dict[str, Any], *, head: int | None = None) -> dict[str, Any]:
    """A consumer as the API shows it, with ``armed`` (a live lease) and ``behind``."""
    out = dict(consumer)
    lease = consumer.get("lease_until")
    out["armed"] = bool(lease) and lease > _iso(_now())
    if head is not None:
        out["behind"] = max(0, head - int(consumer["cursor"]))
    return out


class RelayHubMixin:
    """Consumers and audit. Expects ``self.db``, ``self.waiters`` and ``self._txn``."""

    # ---- plumbing ---------------------------------------------------------

    async def _quiet(self, fn):
        """Run ``fn`` in one immediate transaction without waking waiters.

        Bookkeeping (leases, acks, skipped noise, heartbeats) must not wake
        the dispatcher, SSE readers or other waits, like ``record_delivery``.
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

        return await self.db.write(op)

    @staticmethod
    async def _audit(
        conn,
        actor: str,
        action: str,
        target_kind: str,
        target: str,
        before: Any = None,
        after: Any = None,
    ) -> None:
        await store.insert_audit(
            conn,
            {
                "at": _iso(_now()),
                "actor": actor,
                "action": action,
                "target_kind": target_kind,
                "target": target,
                "before_json": consumers.audit_value(before),
                "after_json": consumers.audit_value(after),
            },
            keep=consumers.AUDIT_KEEP,
        )

    @staticmethod
    async def _consumer_or_404(conn, name: str) -> dict[str, Any]:
        found = await store.get_consumer(conn, name)
        if found is None:
            raise HubError("not_found", f"consumer {name} not found", 404, {"name": name})
        return found

    @staticmethod
    def _require_unmanaged(consumer: dict[str, Any]) -> None:
        if consumer.get("relay_id") is not None:
            raise HubError(
                "managed_by_relay",
                f"consumer {consumer['name']} belongs to a relay route; change the route instead",
                409,
                {"name": consumer["name"], "relay_id": consumer["relay_id"]},
            )

    # ---- consumers --------------------------------------------------------

    async def create_consumer(
        self, actor: Any, body: dict[str, Any], *, relay_id: int | None = None, conn: Any = None
    ) -> dict[str, Any]:
        """Create a durable consumer. ``start`` is ``now`` (default), ``beginning`` or an event id.

        The cursor starts at the head for ``now``, so history is not replayed
        unless asked. ``relay_id`` marks a consumer a relay route owns.
        """
        clean_actor = handle(actor, "actor")
        name = consumers.name(body.get("name"))
        owner = handle(body.get("owner") or clean_actor, "owner")
        description = consumers.description(body.get("description"))
        filters = consumers.filters(body)
        start = body.get("start", "now")

        async def op(conn):
            if await store.get_consumer(conn, name) is not None:
                raise HubError("conflict", f"consumer {name} already exists", 409, {"name": name})
            if await store.count_consumers(conn) >= consumers.CONSUMERS_MAX:
                raise HubError(
                    "limit_exceeded",
                    f"at most {consumers.CONSUMERS_MAX} consumers",
                    409,
                    {"max": consumers.CONSUMERS_MAX},
                )
            head = await store.max_event_id(conn)
            if start in (None, "", "now"):
                cursor = head
            elif start == "beginning":
                cursor = 0
            else:
                cursor = min(optional_id(start, "start") or 0, head)
            now = _iso(_now())
            consumer_id = await store.insert_consumer(
                conn,
                {
                    "name": name,
                    "owner": owner,
                    "description": description,
                    **filters,
                    "cursor": cursor,
                    "relay_id": relay_id,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            created = await store.get_consumer(conn, consumer_id)
            await store.insert_event(
                conn, "consumer.created", None, consumer_id, consumers.event_payload(created, clean_actor), now
            )
            await self._audit(conn, clean_actor, "create", "consumer", name, None, created)
            return {"consumer": public_consumer(created, head=head)}

        return await (op(conn) if conn is not None else self._txn(op))

    async def get_consumer(self, name: Any) -> dict[str, Any]:
        clean = consumers.name(name)

        async def op(conn):
            found = await self._consumer_or_404(conn, clean)
            return {"consumer": public_consumer(found, head=await store.max_event_id(conn))}

        return await self.db.read(op)

    async def list_consumers(self, owner: Any = None) -> dict[str, Any]:
        clean_owner = handle(owner, "owner") if owner not in (None, "") else None

        async def op(conn):
            head = await store.max_event_id(conn)
            rows = await store.list_consumers(conn, owner=clean_owner)
            return {"consumers": [public_consumer(row, head=head) for row in rows], "head": head}

        return await self.db.read(op)

    async def update_consumer(
        self, actor: Any, name: Any, body: dict[str, Any], *, internal: bool = False, conn: Any = None
    ) -> dict[str, Any]:
        """Change filters, ``description`` or ``owner``. Fields left out keep their value."""
        clean_actor = handle(actor, "actor")
        clean = consumers.name(name)
        values: dict[str, Any] = consumers.filters(body, partial=True)
        if "description" in body:
            values["description"] = consumers.description(body.get("description"))
        if body.get("owner") not in (None, ""):
            values["owner"] = handle(body["owner"], "owner")

        async def op(conn):
            before = await self._consumer_or_404(conn, clean)
            if not internal:
                self._require_unmanaged(before)
            if not values:
                return {"consumer": public_consumer(before, head=await store.max_event_id(conn))}
            now = _iso(_now())
            await store.update_consumer(conn, before["id"], {**values, "updated_at": now})
            after = await store.get_consumer(conn, before["id"])
            await store.insert_event(
                conn, "consumer.updated", None, after["id"], consumers.event_payload(after, clean_actor), now
            )
            await self._audit(conn, clean_actor, "update", "consumer", clean, before, after)
            return {"consumer": public_consumer(after, head=await store.max_event_id(conn))}

        return await (op(conn) if conn is not None else self._txn(op))

    async def delete_consumer(
        self, actor: Any, name: Any, *, internal: bool = False, conn: Any = None
    ) -> dict[str, Any]:
        clean_actor = handle(actor, "actor")
        clean = consumers.name(name)

        async def op(conn):
            before = await self._consumer_or_404(conn, clean)
            if not internal:
                self._require_unmanaged(before)
            await store.delete_consumer(conn, before["id"])
            await store.insert_event(
                conn,
                "consumer.deleted",
                None,
                before["id"],
                consumers.event_payload(before, clean_actor),
                _iso(_now()),
            )
            await self._audit(conn, clean_actor, "delete", "consumer", clean, before, None)
            return {"deleted": True, "name": clean}

        return await (op(conn) if conn is not None else self._txn(op))

    async def _scan_consumer(
        self, consumer: dict[str, Any], limit: int
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Matching events after the consumer's cursor, the scanned-to id, and ``has_more``."""
        cursor = int(consumer["cursor"])
        kinds = consumer.get("kinds") or None
        if kinds is None and consumer.get("topics"):
            kinds = ["topic.published"]
        found: list[dict[str, Any]] = []
        scanned = 0
        while True:
            batch = await self.events_after(
                cursor, channel=None, limit=SCAN_BATCH, kinds=frozenset(kinds) if kinds else None
            )
            for event in batch:
                cursor = int(event["id"])
                scanned += 1
                if consumers.matches(consumer, event):
                    found.append(event)
                    if len(found) >= limit:
                        return found, cursor, True
            if len(batch) < SCAN_BATCH:
                return found, cursor, False
            if scanned >= SCAN_MAX:
                return found, cursor, True

    async def consumer_wait(self, name: Any, timeout: Any = None, limit: Any = None) -> dict[str, Any]:
        """Park until events match the consumer after its cursor. Never acks.

        Sets the lease (``armed``) for the timeout plus a grace. ``cursor`` in
        the result is the id to pass to :meth:`consumer_ack` once the events
        are handled. When nothing in range matched, the stored cursor skips
        past the scanned noise so the next wait does not rescan it.
        """
        clean = consumers.name(name)
        seconds = consumers.wait_seconds(timeout)
        clean_limit = consumers.wait_limit(limit)
        deadline = time.monotonic() + seconds
        lease = _iso(_now() + timedelta(seconds=seconds + consumers.LEASE_GRACE))

        async def arm(conn):
            found = await self._consumer_or_404(conn, clean)
            await store.update_consumer(conn, found["id"], {"lease_until": lease})

        await self._quiet(arm)

        async def load(conn):
            return await self._consumer_or_404(conn, clean)

        async with self.waiters.condition:
            while True:
                consumer = await self.db.read(load)
                found, scanned_to, more = await self._scan_consumer(consumer, clean_limit)
                if found:
                    return {
                        "consumer": clean,
                        "events": found,
                        "cursor": int(found[-1]["id"]),
                        "has_more": more,
                        "timed_out": False,
                    }
                if scanned_to > int(consumer["cursor"]):
                    start = int(consumer["cursor"])

                    async def skip(conn, consumer_id=consumer["id"], start=start, to=scanned_to):
                        await store.advance_consumer(conn, consumer_id, expected=start, cursor=to)

                    await self._quiet(skip)
                    if more:
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "consumer": clean,
                        "events": [],
                        "cursor": max(scanned_to, int(consumer["cursor"])),
                        "has_more": False,
                        "timed_out": seconds > 0,
                    }
                try:
                    await asyncio.wait_for(self.waiters.condition.wait(), remaining)
                except asyncio.TimeoutError:
                    continue

    async def consumer_ack(self, name: Any, cursor: Any) -> dict[str, Any]:
        """Move the cursor forward to ``cursor`` (an event id). Never backward; clamped to the head."""
        clean = consumers.name(name)
        target = optional_id(cursor, "cursor")
        if target is None:
            raise HubError("validation_error", "cursor is required", 400, {"field": "cursor"})

        async def op(conn):
            found = await self._consumer_or_404(conn, clean)
            head = await store.max_event_id(conn)
            new = max(int(found["cursor"]), min(target, head))
            now = _iso(_now())
            await store.update_consumer(conn, found["id"], {"cursor": new, "acked_at": now})
            return {"consumer": clean, "cursor": new, "acked": new > int(found["cursor"]), "head": head}

        return await self._quiet(op)

    async def consumer_seek(
        self, actor: Any, name: Any, *, after_event_id: Any = None, at: Any = None
    ) -> dict[str, Any]:
        """Replay: move the cursor to ``after_event_id``, or to the last event before ``at``.

        Exactly one is required. Clamped to the head, audited, and it wakes
        waiters so a parked wait sees the replay at once.
        """
        clean_actor = handle(actor, "actor")
        clean = consumers.name(name)
        has_id = after_event_id not in (None, "")
        has_at = at not in (None, "")
        if has_id == has_at:
            raise HubError(
                "validation_error", "pass exactly one of after_event_id or at", 400, {"field": "after_event_id"}
            )
        target = optional_id(after_event_id, "after_event_id") if has_id else None
        moment = _parse_ts(at, "at") if has_at else None

        async def op(conn):
            before = await self._consumer_or_404(conn, clean)
            head = await store.max_event_id(conn)
            new = min(target, head) if target is not None else await store.event_id_before(conn, moment)
            now = _iso(_now())
            await store.update_consumer(conn, before["id"], {"cursor": new, "updated_at": now})
            after = await store.get_consumer(conn, before["id"])
            await store.insert_event(
                conn, "consumer.seeked", None, after["id"], consumers.event_payload(after, clean_actor), now
            )
            await self._audit(
                conn, clean_actor, "seek", "consumer", clean, {"cursor": before["cursor"]}, {"cursor": new}
            )
            return {"consumer": clean, "cursor": new, "previous": int(before["cursor"]), "head": head}

        return await self._txn(op)

    # ---- audit -------------------------------------------------------------

    async def list_audit(
        self,
        target_kind: Any = None,
        target: Any = None,
        actor: Any = None,
        before_id: Any = None,
        limit: Any = None,
    ) -> dict[str, Any]:
        """Audit rows, newest first. Page with ``before_id`` (the last row's ``id``)."""
        clean_kind = str(target_kind) if target_kind not in (None, "") else None
        clean_target = str(target) if target not in (None, "") else None
        clean_actor = str(actor) if actor not in (None, "") else None
        clean_before = optional_id(before_id, "before_id")
        clean_limit = page_limit(limit)

        async def op(conn):
            rows = await store.list_audit(
                conn,
                target_kind=clean_kind,
                target=clean_target,
                actor=clean_actor,
                before_id=clean_before,
                limit=clean_limit,
            )
            return {"audit": rows, "next_before_id": rows[-1]["id"] if len(rows) == clean_limit else None}

        return await self.db.read(op)
