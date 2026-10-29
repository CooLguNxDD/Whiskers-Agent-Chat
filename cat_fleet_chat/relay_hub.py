"""Business rules for the relay control plane: durable consumers, relays and the audit log.

A mixin of :class:`cat_fleet_chat.hub.Hub`, kept in its own module so the
core hub stays readable. Same layering as the rest of the hub: clean every
argument first, then run one transaction (``_txn`` wakes waiters, ``_quiet``
does not), and record ``consumer.*`` events plus an audit row for every
control-plane change.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from cat_fleet_chat import consumers, relays, store
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.validate import channel_name, handle, optional_id, page_limit

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


# How many parents a relay reply may climb to find the message that came in through it.
REPLY_CHAIN_MAX = 5


class RelayHubMixin:
    """Consumers, relays and audit. Expects ``self.db``, ``self.waiters`` and ``self._txn``."""

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

    # ---- relays ------------------------------------------------------------

    @staticmethod
    async def _relay_or_404(conn, name: str) -> dict[str, Any]:
        found = await store.get_relay(conn, name)
        if found is None:
            raise HubError("not_found", f"relay {name} not found", 404, {"name": name})
        return found

    @staticmethod
    def _check_version(relay: dict[str, Any], expected: int | None) -> None:
        if expected is not None and expected != relay["version"]:
            raise HubError(
                "version_conflict",
                f"relay {relay['name']} changed: it is at version {relay['version']}, you reviewed {expected}",
                412,
                {"version": relay["version"], "expected_version": expected},
            )

    async def _relay_changed(
        self, conn, relay: dict[str, Any], actor: str, change: str, before: Any, after: Any, target: str
    ) -> dict[str, Any]:
        """Bump the version, log ``relay.updated`` and an audit row. Returns the fresh relay."""
        now = _iso(_now())
        await store.bump_relay(conn, relay["id"], now)
        fresh = await store.get_relay(conn, relay["id"])
        await store.insert_event(
            conn, "relay.updated", None, relay["id"], relays.event_payload(fresh, actor, change), now
        )
        kind, _, _ = change.partition(".")
        await self._audit(conn, actor, change, kind if kind != change else "relay", target, before, after)
        return fresh

    async def create_relay(self, actor: Any, body: dict[str, Any]) -> dict[str, Any]:
        """Register a relay. Returns its scoped ``rly_`` token once; only the hash is stored.

        Also creates the relay's built-in replies consumer, which delivers
        messages addressed to ``relay:<name>`` (replies to messages that came
        in through this relay, or explicit ``destination``).
        """
        clean_actor = handle(actor, "actor")
        name = relays.name(body.get("name"))
        platform = relays.platform(body.get("platform"))
        description = relays.description(body.get("description"))
        config = relays.config(body.get("config"), platform)
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HubError("validation_error", "enabled must be true or false", 400, {"field": "enabled"})
        token = relays.new_token()

        async def op(conn):
            if await store.get_relay(conn, name) is not None:
                raise HubError("conflict", f"relay {name} already exists", 409, {"name": name})
            now = _iso(_now())
            relay_id = await store.insert_relay(
                conn,
                {
                    "name": name,
                    "platform": platform,
                    "enabled": int(enabled),
                    "description": description,
                    "config": config,
                    "token_hash": relays.token_hash(token),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await self.create_consumer(
                clean_actor,
                {
                    "name": relays.replies_consumer(name),
                    "owner": clean_actor,
                    "description": f"replies addressed to relay {name}",
                    "kinds": ["message.created"],
                    "destinations": [relays.destination(name)],
                },
                relay_id=relay_id,
                conn=conn,
            )
            relay = await store.get_relay(conn, relay_id)
            await store.insert_event(
                conn, "relay.created", None, relay_id, relays.event_payload(relay, clean_actor, "relay.create"), now
            )
            await self._audit(conn, clean_actor, "relay.create", "relay", name, None, relay)
            return {"relay": relays.public_relay(relay), "token": token}

        return await self._txn(op)

    async def list_relays(self) -> dict[str, Any]:
        async def op(conn):
            return {"relays": [relays.public_relay(row) for row in await store.list_relays(conn)]}

        return await self.db.read(op)

    async def get_relay(self, name: Any) -> dict[str, Any]:
        """A relay with its bindings and routes."""
        clean = relays.name(name)

        async def op(conn):
            return await self._relay_snapshot(conn, await self._relay_or_404(conn, clean))

        return await self.db.read(op)

    async def _relay_snapshot(self, conn, relay: dict[str, Any]) -> dict[str, Any]:
        head = await store.max_event_id(conn)
        routes = []
        for route in await store.list_routes(conn, relay["id"]):
            consumer = await store.get_consumer(conn, route["consumer_id"])
            routes.append(
                {**route, "consumer": public_consumer(consumer, head=head) if consumer else None}
            )
        replies = await store.get_consumer(conn, relays.replies_consumer(relay["name"]))
        return {
            "relay": relays.public_relay(relay),
            "bindings": await store.list_bindings(conn, relay["id"]),
            "routes": routes,
            "replies_consumer": replies["name"] if replies else None,
            "version": relay["version"],
        }

    async def relay_config(self, name: Any) -> dict[str, Any]:
        """Everything a relay process needs to run, at one ``version``."""
        return await self.get_relay(name)

    async def update_relay(
        self, actor: Any, name: Any, body: dict[str, Any], *, expected_version: Any = None
    ) -> dict[str, Any]:
        """Change ``enabled``, ``description`` or ``config`` (top-level keys merge).

        ``expected_version`` (REST ``If-Match``) must be the version the caller
        reviewed; a mismatch is 412 ``version_conflict``.
        """
        clean_actor = handle(actor, "actor")
        clean = relays.name(name)
        expected = relays.expected_version(expected_version, required=True)

        async def op(conn):
            before = await self._relay_or_404(conn, clean)
            self._check_version(before, expected)
            values: dict[str, Any] = {}
            if "enabled" in body:
                if not isinstance(body["enabled"], bool):
                    raise HubError("validation_error", "enabled must be true or false", 400, {"field": "enabled"})
                values["enabled"] = int(body["enabled"])
            if "description" in body:
                values["description"] = relays.description(body.get("description"))
            if "config" in body:
                values["config"] = relays.merge_config(before["config"], body["config"], before["platform"])
            if not values:
                return await self._relay_snapshot(conn, before)
            await store.update_relay(conn, before["id"], values)
            changed = await store.get_relay(conn, before["id"])
            fresh = await self._relay_changed(conn, changed, clean_actor, "relay.update", before, changed, clean)
            return await self._relay_snapshot(conn, fresh)

        return await self._txn(op)

    async def delete_relay(self, actor: Any, name: Any, *, expected_version: Any = None) -> dict[str, Any]:
        """Delete a relay, its bindings, its routes and their consumers. Needs the reviewed version."""
        clean_actor = handle(actor, "actor")
        clean = relays.name(name)
        expected = relays.expected_version(expected_version, required=True)

        async def op(conn):
            before = await self._relay_or_404(conn, clean)
            self._check_version(before, expected)
            owned = await store.list_consumers(conn, relay_id=before["id"])
            await store.delete_relay(conn, before["id"])
            for consumer in owned:
                await self.delete_consumer(clean_actor, consumer["name"], internal=True, conn=conn)
            await store.insert_event(
                conn, "relay.deleted", None, before["id"], relays.event_payload(before, clean_actor, "relay.delete"),
                _iso(_now()),
            )
            await self._audit(conn, clean_actor, "relay.delete", "relay", clean, before, None)
            return {"deleted": True, "name": clean}

        return await self._txn(op)

    async def rotate_relay_token(self, actor: Any, name: Any) -> dict[str, Any]:
        """Issue a new ``rly_`` token; the old one stops working at once. Returned once."""
        clean_actor = handle(actor, "actor")
        clean = relays.name(name)
        token = relays.new_token()

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            await store.update_relay(conn, relay["id"], {"token_hash": relays.token_hash(token), "updated_at": _iso(_now())})
            await self._audit(conn, clean_actor, "relay.rotate_token", "relay", clean, None, {"rotated": True})
            return {"relay": relays.public_relay(await store.get_relay(conn, relay["id"])), "token": token}

        return await self._txn(op)

    async def relay_by_token(self, token: str) -> dict[str, Any] | None:
        """The enabled relay a ``rly_`` token belongs to, for request auth. ``None`` when unknown."""
        if not isinstance(token, str) or not token.startswith(relays.TOKEN_PREFIX):
            return None
        digest = relays.token_hash(token)

        async def op(conn):
            return await store.find_relay_by_token_hash(conn, digest)

        found = await self.db.read(op)
        if found is None or not found["enabled"] or not secrets.compare_digest(found["token_hash"], digest):
            return None
        return found

    async def relay_heartbeat(self, name: Any, status: Any = None) -> dict[str, Any]:
        """Record that a relay is alive, with its status. Does not wake waiters.

        Returns the current config ``version`` and ``enabled``, so the relay
        reloads its config when the version moved.
        """
        clean = relays.name(name)
        clean_status = relays.status(status)

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            await store.update_relay(conn, relay["id"], {"last_seen_at": _iso(_now()), "status": clean_status})
            return {"name": clean, "version": relay["version"], "enabled": relay["enabled"]}

        return await self._quiet(op)

    # ---- bindings ------------------------------------------------------------

    async def add_binding(self, actor: Any, relay_name: Any, body: dict[str, Any]) -> dict[str, Any]:
        """Bind a platform channel or thread to a hub channel. The hub channel must exist."""
        clean_actor = handle(actor, "actor")
        clean = relays.name(relay_name, "relay")
        values = relays.binding(body)
        expected = relays.expected_version(body.get("expected_version"), required=False)

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            self._check_version(relay, expected)
            await self._require_hub_channels(conn, [values["hub_channel"], *values["allowed"]])
            if await store.find_binding(conn, relay["id"], values["external_id"]) is not None:
                raise HubError(
                    "conflict",
                    f"{values['external_id']} is already bound for relay {clean}",
                    409,
                    {"external_id": values["external_id"]},
                )
            if len(await store.list_bindings(conn, relay["id"])) >= relays.BINDINGS_MAX:
                raise HubError("limit_exceeded", f"at most {relays.BINDINGS_MAX} bindings per relay", 409)
            now = _iso(_now())
            binding_id = await store.insert_binding(
                conn, {**values, "relay_id": relay["id"], "created_at": now, "updated_at": now}
            )
            binding = await store.get_binding(conn, binding_id)
            fresh = await self._relay_changed(
                conn, relay, clean_actor, "binding.create", None, binding, f"{clean}/{values['external_id']}"
            )
            return {"binding": binding, "version": fresh["version"]}

        return await self._txn(op)

    async def update_binding(
        self, actor: Any, relay_name: Any, binding_id: Any, body: dict[str, Any]
    ) -> dict[str, Any]:
        clean_actor = handle(actor, "actor")
        clean = relays.name(relay_name, "relay")
        ident = optional_id(binding_id, "binding_id")
        values = relays.binding(body, partial=True)
        values.pop("external_id", None)
        expected = relays.expected_version(body.get("expected_version"), required=False)

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            self._check_version(relay, expected)
            before = await self._binding_or_404(conn, relay, ident)
            names = [values[key] for key in ("hub_channel",) if key in values] + values.get("allowed", [])
            await self._require_hub_channels(conn, names)
            if not values:
                return {"binding": before, "version": relay["version"]}
            await store.update_binding(conn, before["id"], {**values, "updated_at": _iso(_now())})
            after = await store.get_binding(conn, before["id"])
            fresh = await self._relay_changed(
                conn, relay, clean_actor, "binding.update", before, after, f"{clean}/{before['external_id']}"
            )
            return {"binding": after, "version": fresh["version"]}

        return await self._txn(op)

    async def delete_binding(self, actor: Any, relay_name: Any, binding_id: Any) -> dict[str, Any]:
        clean_actor = handle(actor, "actor")
        clean = relays.name(relay_name, "relay")
        ident = optional_id(binding_id, "binding_id")

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            before = await self._binding_or_404(conn, relay, ident)
            await store.delete_binding(conn, before["id"])
            fresh = await self._relay_changed(
                conn, relay, clean_actor, "binding.delete", before, None, f"{clean}/{before['external_id']}"
            )
            return {"deleted": True, "id": before["id"], "version": fresh["version"]}

        return await self._txn(op)

    @staticmethod
    async def _binding_or_404(conn, relay: dict[str, Any], ident: int | None) -> dict[str, Any]:
        found = await store.get_binding(conn, ident) if ident else None
        if found is None or found["relay_id"] != relay["id"]:
            raise HubError("not_found", f"binding {ident} not found", 404, {"id": ident})
        return found

    @staticmethod
    async def _require_hub_channels(conn, names: list[str]) -> None:
        for item in names:
            if await store.get_channel_by_name(conn, channel_name(item)) is None:
                raise HubError("not_found", f"channel {item} not found", 404, {"channel": item})

    # ---- routes --------------------------------------------------------------

    async def add_route(self, actor: Any, relay_name: Any, body: dict[str, Any]) -> dict[str, Any]:
        """Deliver matching events to a platform channel through the relay.

        Creates the route's durable consumer (filters as for consumers, plus
        ``start``) so delivery survives relay restarts and is at-least-once.
        ``format`` is ``line``, ``embed`` or ``card`` (cards carry buttons).
        """
        clean_actor = handle(actor, "actor")
        clean = relays.name(relay_name, "relay")
        external = relays.external_id(body.get("external_id"), "external_id")
        fmt = relays.route_format(body.get("format"))
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HubError("validation_error", "enabled must be true or false", 400, {"field": "enabled"})
        filters = consumers.filters(body)
        expected = relays.expected_version(body.get("expected_version"), required=False)

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            self._check_version(relay, expected)
            if len(await store.list_routes(conn, relay["id"])) >= relays.ROUTES_MAX:
                raise HubError("limit_exceeded", f"at most {relays.ROUTES_MAX} routes per relay", 409)
            created = await self.create_consumer(
                clean_actor,
                {
                    "name": relays.route_consumer(clean, secrets.token_hex(4)),
                    "owner": clean_actor,
                    "description": f"relay {clean} route to {external}",
                    "start": body.get("start", "now"),
                    **filters,
                },
                relay_id=relay["id"],
                conn=conn,
            )
            now = _iso(_now())
            route_id = await store.insert_route(
                conn,
                {
                    "relay_id": relay["id"],
                    "consumer_id": created["consumer"]["id"],
                    "external_id": external,
                    "format": fmt,
                    "enabled": int(enabled),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            route = await store.get_route(conn, route_id)
            shaped = {**route, "consumer": created["consumer"]}
            fresh = await self._relay_changed(conn, relay, clean_actor, "route.create", None, shaped, f"{clean}/{route_id}")
            return {"route": shaped, "version": fresh["version"]}

        return await self._txn(op)

    async def update_route(
        self, actor: Any, relay_name: Any, route_id: Any, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Change ``external_id``, ``format``, ``enabled`` or the consumer filters."""
        clean_actor = handle(actor, "actor")
        clean = relays.name(relay_name, "relay")
        ident = optional_id(route_id, "route_id")
        values: dict[str, Any] = {}
        if "external_id" in body:
            values["external_id"] = relays.external_id(body["external_id"], "external_id")
        if "format" in body:
            values["format"] = relays.route_format(body["format"])
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise HubError("validation_error", "enabled must be true or false", 400, {"field": "enabled"})
            values["enabled"] = int(body["enabled"])
        filters = consumers.filters(body, partial=True)
        expected = relays.expected_version(body.get("expected_version"), required=False)

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            self._check_version(relay, expected)
            before = await self._route_or_404(conn, relay, ident)
            consumer = await store.get_consumer(conn, before["consumer_id"])
            if not values and not filters:
                return {"route": {**before, "consumer": public_consumer(consumer)}, "version": relay["version"]}
            if values:
                await store.update_route(conn, before["id"], {**values, "updated_at": _iso(_now())})
            if filters:
                await self.update_consumer(clean_actor, consumer["name"], filters, internal=True, conn=conn)
            after = {
                **await store.get_route(conn, before["id"]),
                "consumer": public_consumer(await store.get_consumer(conn, before["consumer_id"])),
            }
            fresh = await self._relay_changed(
                conn, relay, clean_actor, "route.update", {**before, "consumer": consumer}, after, f"{clean}/{before['id']}"
            )
            return {"route": after, "version": fresh["version"]}

        return await self._txn(op)

    async def delete_route(self, actor: Any, relay_name: Any, route_id: Any) -> dict[str, Any]:
        clean_actor = handle(actor, "actor")
        clean = relays.name(relay_name, "relay")
        ident = optional_id(route_id, "route_id")

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            before = await self._route_or_404(conn, relay, ident)
            consumer = await store.get_consumer(conn, before["consumer_id"])
            await store.delete_route(conn, before["id"])
            if consumer is not None:
                await self.delete_consumer(clean_actor, consumer["name"], internal=True, conn=conn)
            fresh = await self._relay_changed(conn, relay, clean_actor, "route.delete", before, None, f"{clean}/{before['id']}")
            return {"deleted": True, "id": before["id"], "version": fresh["version"]}

        return await self._txn(op)

    @staticmethod
    async def _route_or_404(conn, relay: dict[str, Any], ident: int | None) -> dict[str, Any]:
        found = await store.get_route(conn, ident) if ident else None
        if found is None or found["relay_id"] != relay["id"]:
            raise HubError("not_found", f"route {ident} not found", 404, {"id": ident})
        return found

    async def relay_owns_consumer(self, relay_name: str, consumer_name: str) -> bool:
        """Whether ``consumer_name`` is one of this relay's route or replies consumers."""

        async def op(conn):
            relay = await store.get_relay(conn, relay_name)
            consumer = await store.get_consumer(conn, consumer_name)
            return relay is not None and consumer is not None and consumer.get("relay_id") == relay["id"]

        return await self.db.read(op)

    # ---- reply targets ---------------------------------------------------------

    async def relay_reply_target(self, relay_name: Any, message_id: Any) -> dict[str, Any]:
        """Where a relay should post a message addressed to it.

        Walks the reply chain for the nearest message that came in through
        this relay and answers in its thread (or channel), quoting it. With
        none, falls back to the first outbound binding of the message's hub
        channel. ``target`` is null when there is nowhere to post it.
        """
        clean = relays.name(relay_name, "relay")
        ident = optional_id(message_id, "message_id")

        async def op(conn):
            relay = await self._relay_or_404(conn, clean)
            message = await store.get_message(conn, ident) if ident else None
            if message is None:
                raise HubError("not_found", f"message {ident} not found", 404, {"id": ident})
            current = await store.get_message(conn, message["reply_to"]) if message.get("reply_to") else None
            for _ in range(REPLY_CHAIN_MAX):
                if current is None:
                    break
                origin = current.get("origin") or {}
                if origin.get("relay") == clean:
                    return {
                        "message_id": message["id"],
                        "target": {
                            "external_id": origin.get("thread_id") or origin["channel_id"],
                            "reply_to_external": origin.get("message_id"),
                            "via": "origin",
                        },
                    }
                reply_to = current.get("reply_to")
                current = await store.get_message(conn, reply_to) if reply_to else None
            for binding in await store.list_bindings(conn, relay["id"]):
                if binding["hub_channel"] == message["channel"] and binding["direction"] in ("out", "both"):
                    return {
                        "message_id": message["id"],
                        "target": {"external_id": binding["external_id"], "reply_to_external": None, "via": "binding"},
                    }
            return {"message_id": message["id"], "target": None}

        return await self.db.read(op)
