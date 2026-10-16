"""All SQL for channels, messages, mentions, tasks, attachments, mail, the event stream, and search.

Statements are built with SQLAlchemy Core (see ``schema.py``) and run on the
caller's aiosqlite connection. Callers own the transaction. This module does
not commit, notify waiters, or enforce business rules.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
from sqlalchemy import bindparam, case, delete, func, insert, literal, select, text, union_all, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from cat_fleet_chat.mailbox import mail_row
from cat_fleet_chat.schema import (
    SEARCH_INDEX,
    SEARCH_KIND_CODES,
    SEARCH_ROWID_STRIDE,
    attachments,
    channels,
    embeddings,
    events,
    fetch_all,
    fetch_one,
    idempotency,
    mailbox,
    mentions,
    messages,
    run,
    search_rowid,
    task_events,
    tasks,
    webhooks,
)

CHANNEL_COLUMNS = (
    channels.c.id,
    channels.c.name,
    channels.c.topic,
    channels.c.created_at,
    channels.c.archived_at,
    channels.c.archived_by,
    channels.c.state,
    channels.c.state_note,
    channels.c.state_updated_at,
    channels.c.state_updated_by,
)

ATTACHMENT_COLUMNS = (
    attachments.c.id,
    attachments.c.message_id,
    attachments.c.filename,
    attachments.c.content_type,
    attachments.c.size_bytes,
    attachments.c.storage,
    attachments.c.bucket,
    attachments.c.object_key,
    attachments.c.sha256,
    attachments.c.created_at,
)

TASK_COLUMNS = (
    tasks.c.id,
    tasks.c.channel_id,
    channels.c.name.label("channel"),
    tasks.c.title,
    tasks.c.description,
    tasks.c.status,
    tasks.c.assignee,
    tasks.c.version,
    tasks.c.created_at,
    tasks.c.updated_at,
)


def _message_select(source=messages):
    """Message columns plus the channel name, from ``messages`` or a subquery of it."""
    return select(
        source.c.id,
        source.c.channel_id,
        channels.c.name.label("channel"),
        source.c.author,
        source.c.text,
        source.c.reply_to,
        source.c.created_at,
        source.c.destination,
        source.c.origin_json,
    ).join_from(source, channels, channels.c.id == source.c.channel_id)


def _message_row(
    row: aiosqlite.Row, mention_names: list[str], files: list[dict[str, Any]]
) -> dict[str, Any]:
    origin_json = row["origin_json"]
    return {
        "id": row["id"],
        "channel_id": row["channel_id"],
        "channel": row["channel"],
        "author": row["author"],
        "text": row["text"],
        "reply_to": row["reply_to"],
        "created_at": row["created_at"],
        "mentions": mention_names,
        "attachments": files,
        # Routing metadata: where it came from (Discord relay) and which
        # outbound webhook it is addressed to. Both are null for plain messages.
        "origin": json.loads(origin_json) if origin_json else None,
        "destination": row["destination"],
    }


async def _mentions_for(conn: aiosqlite.Connection, message_ids: list[int]) -> dict[int, list[str]]:
    if not message_ids:
        return {}
    rows = await fetch_all(
        conn,
        select(mentions.c.message_id, mentions.c.agent_name)
        .where(mentions.c.message_id.in_(message_ids))
        .order_by(mentions.c.message_id, mentions.c.agent_name),
    )
    found: dict[int, list[str]] = {}
    for row in rows:
        found.setdefault(row["message_id"], []).append(row["agent_name"])
    return found


async def _attachments_for(
    conn: aiosqlite.Connection, message_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    if not message_ids:
        return {}
    rows = await fetch_all(
        conn,
        select(*ATTACHMENT_COLUMNS)
        .where(attachments.c.message_id.in_(message_ids))
        .order_by(attachments.c.message_id, attachments.c.id),
    )
    found: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row["message_id"], []).append(dict(row))
    return found


async def messages_from_rows(conn: aiosqlite.Connection, rows: list[aiosqlite.Row]) -> list[dict[str, Any]]:
    """Attach mention handles and attachments to message rows, preserving row order."""
    ids = [row["id"] for row in rows]
    grouped = await _mentions_for(conn, ids)
    files = await _attachments_for(conn, ids)
    return [
        _message_row(row, grouped.get(row["id"], []), files.get(row["id"], []))
        for row in rows
    ]


async def get_channel_by_name(conn: aiosqlite.Connection, name: str) -> dict[str, Any] | None:
    row = await fetch_one(conn, select(*CHANNEL_COLUMNS).where(channels.c.name == name))
    return dict(row) if row else None


async def get_channel_by_id(conn: aiosqlite.Connection, channel_id: int) -> dict[str, Any] | None:
    row = await fetch_one(conn, select(*CHANNEL_COLUMNS).where(channels.c.id == channel_id))
    return dict(row) if row else None


async def list_channels(
    conn: aiosqlite.Connection,
    *,
    include_archived: bool = False,
    states: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Channels by name. ``states`` filters; archived ones need ``include_archived`` or that state."""
    stmt = select(*CHANNEL_COLUMNS).order_by(channels.c.name)
    wanted = sorted(states or ())
    if wanted:
        stmt = stmt.where(channels.c.state.in_(wanted))
    elif not include_archived:
        stmt = stmt.where(channels.c.state != "archived")
    return [dict(row) for row in await fetch_all(conn, stmt)]


async def insert_channel(
    conn: aiosqlite.Connection, name: str, topic: str, created_at: str
) -> int:
    cursor = await run(
        conn, insert(channels).values(name=name, topic=topic, created_at=created_at)
    )
    return int(cursor.lastrowid)


async def update_channel_state(
    conn: aiosqlite.Connection,
    channel_id: int,
    *,
    state: str,
    note: str,
    updated_at: str,
    updated_by: str,
) -> None:
    """Set the lifecycle state. ``archived`` also stamps archived_at/by; any other state clears them."""
    archived = state == "archived"
    await run(
        conn,
        update(channels)
        .where(channels.c.id == channel_id)
        .values(
            state=state,
            state_note=note,
            state_updated_at=updated_at,
            state_updated_by=updated_by,
            archived_at=updated_at if archived else None,
            archived_by=updated_by if archived else None,
        ),
    )


async def get_idempotency(conn: aiosqlite.Connection, key: str) -> dict[str, Any] | None:
    row = await fetch_one(
        conn,
        select(
            idempotency.c.client_request_id,
            idempotency.c.kind,
            idempotency.c.fingerprint,
            idempotency.c.result_json,
            idempotency.c.created_at,
        ).where(idempotency.c.client_request_id == key),
    )
    if row is None:
        return None
    return {
        "client_request_id": row["client_request_id"],
        "kind": row["kind"],
        "fingerprint": row["fingerprint"],
        "result": json.loads(row["result_json"]),
        "created_at": row["created_at"],
    }


async def insert_idempotency(
    conn: aiosqlite.Connection,
    key: str,
    kind: str,
    fingerprint: str,
    result: dict[str, Any],
    created_at: str,
) -> None:
    await run(
        conn,
        insert(idempotency).values(
            client_request_id=key,
            kind=kind,
            fingerprint=fingerprint,
            result_json=json.dumps(result, separators=(",", ":")),
            created_at=created_at,
        ),
    )


async def get_message(conn: aiosqlite.Connection, message_id: int) -> dict[str, Any] | None:
    row = await fetch_one(conn, _message_select().where(messages.c.id == message_id))
    if row is None:
        return None
    packed = await messages_from_rows(conn, [row])
    return packed[0]


async def insert_message(
    conn: aiosqlite.Connection,
    channel_id: int,
    author: str,
    text: str,
    reply_to: int | None,
    created_at: str,
    mention_names: list[str],
    files: Iterable[dict[str, Any]] = (),
    destination: str | None = None,
    origin: dict[str, Any] | None = None,
) -> int:
    cursor = await run(
        conn,
        insert(messages).values(
            channel_id=channel_id,
            author=author,
            text=text,
            reply_to=reply_to,
            created_at=created_at,
            destination=destination,
            origin_json=json.dumps(origin, separators=(",", ":")) if origin else None,
        ),
    )
    message_id = int(cursor.lastrowid)
    for name in mention_names:
        await run(conn, insert(mentions).values(message_id=message_id, agent_name=name))
    for item in files:
        await run(
            conn,
            insert(attachments).values(message_id=message_id, created_at=created_at, **item),
        )
    return message_id


async def get_attachment(conn: aiosqlite.Connection, attachment_id: int) -> dict[str, Any] | None:
    """One attachment with the channel and author of the message that carries it."""
    row = await fetch_one(
        conn,
        select(
            *ATTACHMENT_COLUMNS,
            channels.c.name.label("channel"),
            messages.c.author.label("uploaded_by"),
        )
        .join_from(attachments, messages, messages.c.id == attachments.c.message_id)
        .join(channels, channels.c.id == messages.c.channel_id)
        .where(attachments.c.id == attachment_id),
    )
    return dict(row) if row else None


async def list_messages(
    conn: aiosqlite.Connection,
    channel_id: int,
    *,
    since_id: int | None,
    before_id: int | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Return up to ``limit`` messages in ascending id order, plus ``has_more``."""
    extra = limit + 1
    if since_id is not None:
        rows = await fetch_all(
            conn,
            _message_select()
            .where(messages.c.channel_id == channel_id, messages.c.id > since_id)
            .order_by(messages.c.id.asc())
            .limit(extra),
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        # Newest page first in a subquery, then flip it back to ascending.
        newest = select(
            messages.c.id,
            messages.c.channel_id,
            messages.c.author,
            messages.c.text,
            messages.c.reply_to,
            messages.c.created_at,
            messages.c.destination,
            messages.c.origin_json,
        ).where(messages.c.channel_id == channel_id)
        if before_id is not None:
            newest = newest.where(messages.c.id < before_id)
        page = newest.order_by(messages.c.id.desc()).limit(extra).subquery("m")
        rows = await fetch_all(conn, _message_select(page).order_by(page.c.id.asc()))
        has_more = len(rows) > limit
        if has_more:
            rows = rows[-limit:]
    return await messages_from_rows(conn, rows), has_more


async def mentions_after(
    conn: aiosqlite.Connection,
    agent: str,
    since_id: int,
    *,
    channel: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    stmt = (
        _message_select()
        .join(mentions, mentions.c.message_id == messages.c.id)
        .where(mentions.c.agent_name == agent, messages.c.id > since_id)
    )
    if channel is not None:
        stmt = stmt.where(channels.c.name == channel)
    rows = await fetch_all(conn, stmt.order_by(messages.c.id.asc()).limit(limit + 1))
    has_more = len(rows) > limit
    return await messages_from_rows(conn, rows[:limit]), has_more


async def max_event_id(conn: aiosqlite.Connection) -> int:
    row = await fetch_one(conn, select(func.coalesce(func.max(events.c.id), 0).label("n")))
    return int(row["n"])


async def event_exists(conn: aiosqlite.Connection, event_id: int) -> bool:
    return await fetch_one(conn, select(literal(1)).where(events.c.id == event_id)) is not None


async def min_event_id(conn: aiosqlite.Connection) -> int | None:
    row = await fetch_one(conn, select(func.min(events.c.id).label("n")))
    if row is None or row["n"] is None:
        return None
    return int(row["n"])


async def insert_event(
    conn: aiosqlite.Connection,
    kind: str,
    channel_id: int | None,
    entity_id: int,
    payload: dict[str, Any],
    created_at: str,
) -> int:
    cursor = await run(
        conn,
        insert(events).values(
            kind=kind,
            channel_id=channel_id,
            entity_id=entity_id,
            payload_json=json.dumps(payload, separators=(",", ":")),
            created_at=created_at,
        ),
    )
    return int(cursor.lastrowid)


def _event_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "channel_id": row["channel_id"],
        "channel": row["channel"],
        "entity_id": row["entity_id"],
        "payload": json.loads(row["payload_json"]),
        "created_at": row["created_at"],
    }


async def events_after(
    conn: aiosqlite.Connection,
    after_event_id: int,
    *,
    channel: str | None,
    limit: int,
    kinds: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            events.c.id,
            events.c.kind,
            events.c.channel_id,
            channels.c.name.label("channel"),
            events.c.entity_id,
            events.c.payload_json,
            events.c.created_at,
        )
        .join_from(events, channels, channels.c.id == events.c.channel_id, isouter=True)
        .where(events.c.id > after_event_id)
    )
    if channel is not None:
        stmt = stmt.where(channels.c.name == channel)
    if kinds:
        stmt = stmt.where(events.c.kind.in_(sorted(kinds)))
    rows = await fetch_all(conn, stmt.order_by(events.c.id.asc()).limit(limit))
    return [_event_row(row) for row in rows]


async def insert_mail(
    conn: aiosqlite.Connection,
    *,
    recipient: str,
    sender: str,
    subject: str,
    body: str,
    reply_to: int | None,
    created_at: str,
    message_id: int | None = None,
) -> int:
    cursor = await run(
        conn,
        insert(mailbox).values(
            recipient=recipient,
            sender=sender,
            subject=subject,
            body=body,
            reply_to=reply_to,
            created_at=created_at,
            message_id=message_id,
        ),
    )
    return int(cursor.lastrowid)


async def get_mail(conn: aiosqlite.Connection, mail_id: int) -> dict[str, Any] | None:
    row = await fetch_one(conn, select(mailbox).where(mailbox.c.id == mail_id))
    return None if row is None else mail_row(row)


async def get_mail_many(conn: aiosqlite.Connection, mail_ids: Iterable[int]) -> list[dict[str, Any]]:
    wanted = sorted(set(mail_ids))
    if not wanted:
        return []
    rows = await fetch_all(
        conn, select(mailbox).where(mailbox.c.id.in_(wanted)).order_by(mailbox.c.id.asc())
    )
    return [mail_row(row) for row in rows]


async def list_mail(
    conn: aiosqlite.Connection,
    recipient: str,
    *,
    state: str,
    since_id: int,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Mail for ``recipient`` after ``since_id``, ascending, plus ``has_more``.

    ``state`` is ``unread`` (never read), ``read`` (read or acked) or ``all``.
    """
    stmt = select(mailbox).where(mailbox.c.recipient == recipient, mailbox.c.id > since_id)
    if state == "unread":
        stmt = stmt.where(mailbox.c.read_at.is_(None))
    elif state == "read":
        stmt = stmt.where(mailbox.c.read_at.is_not(None))
    rows = await fetch_all(conn, stmt.order_by(mailbox.c.id.asc()).limit(limit + 1))
    return [mail_row(row) for row in rows[:limit]], len(rows) > limit


async def max_mail_id(conn: aiosqlite.Connection, recipient: str) -> int:
    row = await fetch_one(
        conn,
        select(func.coalesce(func.max(mailbox.c.id), 0).label("n")).where(
            mailbox.c.recipient == recipient
        ),
    )
    return int(row["n"])


async def mail_counts(conn: aiosqlite.Connection, recipient: str) -> dict[str, int]:
    row = await fetch_one(
        conn,
        select(
            func.count().label("total"),
            func.coalesce(
                func.sum(case((mailbox.c.read_at.is_(None), 1), else_=0)), 0
            ).label("unread"),
            func.coalesce(
                func.sum(case((mailbox.c.acked_at.is_(None), 1), else_=0)), 0
            ).label("unacked"),
        ).where(mailbox.c.recipient == recipient),
    )
    return {"unread": int(row["unread"]), "unacked": int(row["unacked"]), "total": int(row["total"])}


async def mark_mail_read(
    conn: aiosqlite.Connection,
    recipient: str,
    *,
    mail_ids: list[int] | None,
    up_to_id: int | None,
    read_at: str,
) -> list[int]:
    """Stamp ``read_at`` on unread mail of ``recipient``. Returns the ids that changed."""
    where = [mailbox.c.recipient == recipient, mailbox.c.read_at.is_(None)]
    if mail_ids is not None:
        where.append(mailbox.c.id.in_(mail_ids))
    if up_to_id is not None:
        where.append(mailbox.c.id <= up_to_id)
    changed = [
        int(row["id"])
        for row in await fetch_all(conn, select(mailbox.c.id).where(*where).order_by(mailbox.c.id))
    ]
    if changed:
        await run(conn, update(mailbox).where(mailbox.c.id.in_(changed)).values(read_at=read_at))
    return changed


async def ack_mail(
    conn: aiosqlite.Connection, recipient: str, mail_ids: list[int], acked_at: str
) -> list[int]:
    """Stamp ``acked_at`` (and ``read_at`` when unset). Returns the ids that changed."""
    where = (
        mailbox.c.recipient == recipient,
        mailbox.c.id.in_(mail_ids),
        mailbox.c.acked_at.is_(None),
    )
    changed = [
        int(row["id"])
        for row in await fetch_all(conn, select(mailbox.c.id).where(*where).order_by(mailbox.c.id))
    ]
    if changed:
        await run(
            conn,
            update(mailbox)
            .where(mailbox.c.id.in_(changed))
            .values(acked_at=acked_at, read_at=func.coalesce(mailbox.c.read_at, acked_at)),
        )
    return changed


_WEBHOOK_LISTS = ("kinds", "channels", "mentions", "exclude_authors")


def _webhook_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Full webhook including ``secret``. Use :func:`public_webhook` for API output."""
    body = dict(row)
    for name in _WEBHOOK_LISTS:
        body[name] = json.loads(body.pop(f"{name}_json"))
    body["enabled"] = bool(body["enabled"])
    body["allow_override"] = bool(body["allow_override"])
    body["directed_only"] = bool(body["directed_only"])
    return body


def redact_url(url: str) -> str:
    """Scheme, host and port only. A Discord or Slack hook URL carries its token in the path."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "<invalid url>"
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}{port}/…"


def public_webhook(hook: dict[str, Any]) -> dict[str, Any]:
    """A webhook as the API shows it: no secret, a redacted URL, only the fields its direction uses."""
    out = {key: value for key, value in hook.items() if key != "secret"}
    if out.get("url"):
        out["url"] = redact_url(out["url"])
    if hook["direction"] == "out":
        for key in ("channel", "author", "allow_override"):
            out.pop(key, None)
    else:
        for key in (
            "url",
            "format",
            *_WEBHOOK_LISTS,
            "cursor",
            "failure_count",
            "last_status",
            "last_error",
            "last_delivery_at",
            "description",
            "directed_only",
            "discord_channel_id",
        ):
            out.pop(key, None)
    return out


async def insert_webhook(conn: aiosqlite.Connection, values: dict[str, Any]) -> int:
    row = dict(values)
    for name in _WEBHOOK_LISTS:
        if name in row:
            row[f"{name}_json"] = json.dumps(row.pop(name), separators=(",", ":"))
    cursor = await run(conn, insert(webhooks).values(**row))
    return int(cursor.lastrowid)


async def get_webhook(conn: aiosqlite.Connection, ident: int | str) -> dict[str, Any] | None:
    """One webhook by numeric id or by name."""
    column = webhooks.c.id if isinstance(ident, int) else webhooks.c.name
    row = await fetch_one(conn, select(webhooks).where(column == ident))
    return None if row is None else _webhook_row(row)


async def list_webhooks(
    conn: aiosqlite.Connection,
    *,
    direction: str | None = None,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    stmt = select(webhooks)
    if direction is not None:
        stmt = stmt.where(webhooks.c.direction == direction)
    if enabled_only:
        stmt = stmt.where(webhooks.c.enabled == 1)
    rows = await fetch_all(conn, stmt.order_by(webhooks.c.id.asc()))
    return [_webhook_row(row) for row in rows]


async def find_destination_hook(
    conn: aiosqlite.Connection, discord_channel_id: str
) -> dict[str, Any] | None:
    """The enabled outbound hook attached to this Discord channel, if any. Oldest wins."""
    row = await fetch_one(
        conn,
        select(webhooks)
        .where(
            webhooks.c.direction == "out",
            webhooks.c.enabled == 1,
            webhooks.c.discord_channel_id == discord_channel_id,
        )
        .order_by(webhooks.c.id.asc())
        .limit(1),
    )
    return None if row is None else _webhook_row(row)


async def update_webhook(conn: aiosqlite.Connection, hook_id: int, values: dict[str, Any]) -> None:
    row = dict(values)
    for name in _WEBHOOK_LISTS:
        if name in row:
            row[f"{name}_json"] = json.dumps(row.pop(name), separators=(",", ":"))
    await run(conn, update(webhooks).where(webhooks.c.id == hook_id).values(**row))


async def delete_webhook(conn: aiosqlite.Connection, hook_id: int) -> None:
    await run(conn, delete(webhooks).where(webhooks.c.id == hook_id))


async def insert_task(
    conn: aiosqlite.Connection,
    channel_id: int,
    title: str,
    description: str,
    status: str,
    assignee: str | None,
    created_at: str,
) -> int:
    cursor = await run(
        conn,
        insert(tasks).values(
            channel_id=channel_id,
            title=title,
            description=description,
            status=status,
            assignee=assignee,
            version=1,
            created_at=created_at,
            updated_at=created_at,
        ),
    )
    return int(cursor.lastrowid)


async def insert_task_event(
    conn: aiosqlite.Connection,
    task_id: int,
    actor: str,
    from_status: str | None,
    to_status: str,
    note: str,
    created_at: str,
) -> int:
    cursor = await run(
        conn,
        insert(task_events).values(
            task_id=task_id,
            actor=actor,
            from_status=from_status,
            to_status=to_status,
            note=note,
            created_at=created_at,
        ),
    )
    return int(cursor.lastrowid)


def _task_select():
    return select(*TASK_COLUMNS).join_from(tasks, channels, channels.c.id == tasks.c.channel_id)


async def get_task_row(conn: aiosqlite.Connection, task_id: int) -> aiosqlite.Row | None:
    return await fetch_one(conn, _task_select().where(tasks.c.id == task_id))


async def task_events_for(conn: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    rows = await fetch_all(
        conn,
        select(
            task_events.c.id,
            task_events.c.task_id,
            task_events.c.actor,
            task_events.c.from_status,
            task_events.c.to_status,
            task_events.c.note,
            task_events.c.created_at,
        )
        .where(task_events.c.task_id == task_id)
        .order_by(task_events.c.id.asc()),
    )
    return [dict(row) for row in rows]


async def pack_task(conn: aiosqlite.Connection, row: aiosqlite.Row) -> dict[str, Any]:
    body = dict(row)
    body["events"] = await task_events_for(conn, int(row["id"]))
    return body


async def get_task(conn: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    row = await get_task_row(conn, task_id)
    if row is None:
        return None
    return await pack_task(conn, row)


async def update_task(
    conn: aiosqlite.Connection,
    task_id: int,
    *,
    status: str,
    assignee: str | None,
    version: int,
    updated_at: str,
    expected_version: int,
) -> bool:
    """Conditional update. Returns whether this version won."""
    cursor = await run(
        conn,
        update(tasks)
        .where(tasks.c.id == task_id, tasks.c.version == expected_version)
        .values(status=status, assignee=assignee, version=version, updated_at=updated_at),
    )
    return cursor.rowcount == 1


async def claim_open_task(
    conn: aiosqlite.Connection,
    task_id: int,
    assignee: str,
    updated_at: str,
    expected_version: int | None,
) -> bool:
    """Claim only an unassigned open task. Optional version guard."""
    stmt = update(tasks).where(
        tasks.c.id == task_id,
        tasks.c.status == "open",
        tasks.c.assignee.is_(None),
    )
    if expected_version is not None:
        stmt = stmt.where(tasks.c.version == expected_version)
    cursor = await run(
        conn,
        stmt.values(
            status="claimed",
            assignee=assignee,
            version=tasks.c.version + 1,
            updated_at=updated_at,
        ),
    )
    return cursor.rowcount == 1


async def list_tasks(
    conn: aiosqlite.Connection,
    *,
    channel: str | None,
    status: str | None,
    assignee: str | None,
    exclude_statuses: Iterable[str] = (),
) -> list[dict[str, Any]]:
    stmt = _task_select()
    if channel is not None:
        stmt = stmt.where(channels.c.name == channel)
    if status is not None:
        stmt = stmt.where(tasks.c.status == status)
    if assignee is not None:
        stmt = stmt.where(tasks.c.assignee == assignee)
    skipped = sorted(exclude_statuses)
    if skipped:
        stmt = stmt.where(tasks.c.status.not_in(skipped))
    rows = await fetch_all(conn, stmt.order_by(tasks.c.id.asc()))
    return [await pack_task(conn, row) for row in rows]


# ---- full-text search (FTS5, schema v6) ----------------------------------
#
# The index is a virtual table, so these statements are ``text()`` rather than
# Core table expressions. Every value is a bound parameter; the MATCH string
# is built by ``search.build_match`` and never comes raw from a caller.

_INDEX_COLUMNS = (
    f"INSERT INTO {SEARCH_INDEX} (rowid, title, body, kind, ref_id, channel_id, author, created_at) "
)

_INDEX_MESSAGE = text(
    _INDEX_COLUMNS
    + f"SELECT m.id * {SEARCH_ROWID_STRIDE} + {SEARCH_KIND_CODES['message']}, '', m.text, "
    "'message', m.id, m.channel_id, m.author, m.created_at FROM messages m WHERE m.id = :ref_id"
)

_INDEX_TASK = text(
    _INDEX_COLUMNS
    + f"SELECT t.id * {SEARCH_ROWID_STRIDE} + {SEARCH_KIND_CODES['task']}, t.title, t.description, "
    "'task', t.id, t.channel_id, COALESCE(t.assignee, (SELECT e.actor FROM task_events e "
    "WHERE e.task_id = t.id ORDER BY e.id DESC LIMIT 1), ''), t.created_at "
    "FROM tasks t WHERE t.id = :ref_id"
)

_INDEX_ATTACHMENT = text(
    _INDEX_COLUMNS
    + f"SELECT a.id * {SEARCH_ROWID_STRIDE} + {SEARCH_KIND_CODES['attachment']}, a.filename, "
    "a.content_type, 'attachment', a.id, m.channel_id, m.author, a.created_at "
    "FROM attachments a JOIN messages m ON m.id = a.message_id WHERE a.id = :ref_id"
)

_INDEX_SOURCES = {"message": _INDEX_MESSAGE, "task": _INDEX_TASK, "attachment": _INDEX_ATTACHMENT}

_UNINDEX = text(f"DELETE FROM {SEARCH_INDEX} WHERE rowid = :rowid")


async def unindex(conn: aiosqlite.Connection, kind: str, ref_id: int) -> None:
    await run(conn, _UNINDEX.bindparams(rowid=search_rowid(kind, ref_id)))


async def reindex(conn: aiosqlite.Connection, kind: str, ref_id: int) -> None:
    """Replace the index entry for one source row with its current contents."""
    await unindex(conn, kind, ref_id)
    await run(conn, _INDEX_SOURCES[kind].bindparams(ref_id=ref_id))


async def index_message(conn: aiosqlite.Connection, message_id: int) -> None:
    """Index a message and every attachment it carries."""
    await reindex(conn, "message", message_id)
    rows = await fetch_all(
        conn, select(attachments.c.id).where(attachments.c.message_id == message_id)
    )
    for row in rows:
        await reindex(conn, "attachment", int(row["id"]))


def _search_where(
    *,
    kinds: Iterable[str] | None,
    channel_id: int | None,
    author: str | None,
    since: str | None,
    match: bool = True,
) -> tuple[str, dict[str, Any]]:
    clauses = [f"{SEARCH_INDEX} MATCH :match"] if match else ["1 = 1"]
    params: dict[str, Any] = {}
    wanted = sorted(kinds or ())
    if wanted:
        clauses.append(f"{SEARCH_INDEX}.kind IN :kinds")
        params["kinds"] = wanted
    if channel_id is not None:
        clauses.append(f"{SEARCH_INDEX}.channel_id = :channel_id")
        params["channel_id"] = channel_id
    if author is not None:
        clauses.append(f"{SEARCH_INDEX}.author = :author")
        params["author"] = author
    if since is not None:
        clauses.append(f"{SEARCH_INDEX}.created_at >= :since")
        params["since"] = since
    return " AND ".join(clauses), params


def _bind(sql: str, params: dict[str, Any]):
    stmt = text(sql)
    if "kinds" in params:
        stmt = stmt.bindparams(bindparam("kinds", expanding=True))
    return stmt.bindparams(**params)


async def search_index(
    conn: aiosqlite.Connection,
    match: str,
    *,
    kinds: Iterable[str] | None,
    channel_id: int | None,
    author: str | None,
    since: str | None,
    limit: int,
    offset: int,
    weights: tuple[float, float],
    snippet_markers: tuple[str, str, str],
    snippet_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    """One page of BM25-ranked hits plus the total match count.

    ``bm25()`` is lower-is-better; rows come back best first with the raw
    value in ``rank``. Ties go to the newer row.
    """
    where, params = _search_where(kinds=kinds, channel_id=channel_id, author=author, since=since)
    params["match"] = match
    title_weight, body_weight = weights
    opening, closing, ellipsis = snippet_markers
    page_sql = (
        f"SELECT {SEARCH_INDEX}.kind AS kind, {SEARCH_INDEX}.ref_id AS ref_id, "
        f"{SEARCH_INDEX}.channel_id AS channel_id, channels.name AS channel, "
        f"{SEARCH_INDEX}.author AS author, {SEARCH_INDEX}.created_at AS created_at, "
        f"snippet({SEARCH_INDEX}, -1, :open, :close, :ellipsis, :tokens) AS snippet, "
        f"bm25({SEARCH_INDEX}, {float(title_weight)}, {float(body_weight)}) AS bm25_rank "
        f"FROM {SEARCH_INDEX} LEFT JOIN channels ON channels.id = {SEARCH_INDEX}.channel_id "
        f"WHERE {where} ORDER BY bm25_rank ASC, {SEARCH_INDEX}.created_at DESC, {SEARCH_INDEX}.rowid DESC "
        "LIMIT :limit OFFSET :offset"
    )
    page_params = {
        **params,
        "open": opening,
        "close": closing,
        "ellipsis": ellipsis,
        "tokens": snippet_tokens,
        "limit": limit,
        "offset": offset,
    }
    rows = await fetch_all(conn, _bind(page_sql, page_params))
    count_row = await fetch_one(
        conn, _bind(f"SELECT count(*) AS n FROM {SEARCH_INDEX} WHERE {where}", params)
    )
    total = int(count_row["n"]) if count_row is not None else 0
    return [dict(row) for row in rows], total


# ---- embeddings (schema v9) ----------------------------------------------
#
# Every join drives from search_index and looks embeddings up by its primary
# key. ``CROSS JOIN`` pins that order: SQLite never reorders it, so the
# virtual table is scanned once instead of once per embedding.

_EMBED_JOIN = (
    f"embeddings.kind = {SEARCH_INDEX}.kind AND embeddings.ref_id = {SEARCH_INDEX}.ref_id "
    "AND embeddings.model = :model"
)


def _dim_clause(dim: int | None, params: dict[str, Any]) -> str:
    if dim is None:
        return ""
    params["dim"] = dim
    return " AND embeddings.dim = :dim"


async def embed_missing(
    conn: aiosqlite.Connection, model: str, dim: int | None, limit: int
) -> list[dict[str, Any]]:
    """Index entries with no vector for ``model`` (of width ``dim``, when known), oldest first."""
    params: dict[str, Any] = {"model": model, "limit": limit}
    on = _EMBED_JOIN + _dim_clause(dim, params)
    sql = (
        f"SELECT {SEARCH_INDEX}.kind AS kind, {SEARCH_INDEX}.ref_id AS ref_id, "
        f"{SEARCH_INDEX}.title AS title, {SEARCH_INDEX}.body AS body "
        f"FROM {SEARCH_INDEX} LEFT JOIN embeddings ON {on} "
        f"WHERE embeddings.ref_id IS NULL ORDER BY {SEARCH_INDEX}.rowid ASC LIMIT :limit"
    )
    return [dict(row) for row in await fetch_all(conn, _bind(sql, params))]


async def index_entries(
    conn: aiosqlite.Connection, keys: Iterable[tuple[str, int]], model: str
) -> list[dict[str, Any]]:
    """Current index text for ``keys`` plus the stored hash for ``model`` (``None`` when absent).

    Keys whose row is gone from the index (deleted source) are left out.
    """
    rowids = sorted({search_rowid(kind, ref_id) for kind, ref_id in keys if kind in SEARCH_KIND_CODES})
    if not rowids:
        return []
    sql = (
        f"SELECT {SEARCH_INDEX}.kind AS kind, {SEARCH_INDEX}.ref_id AS ref_id, "
        f"{SEARCH_INDEX}.title AS title, {SEARCH_INDEX}.body AS body, "
        "embeddings.text_hash AS text_hash, embeddings.dim AS dim "
        f"FROM {SEARCH_INDEX} LEFT JOIN embeddings ON {_EMBED_JOIN} "
        f"WHERE {SEARCH_INDEX}.rowid IN :rowids"
    )
    stmt = text(sql).bindparams(bindparam("rowids", expanding=True)).bindparams(
        rowids=rowids, model=model
    )
    return [dict(row) for row in await fetch_all(conn, stmt)]


async def embedded_page(
    conn: aiosqlite.Connection, model: str, after_rowid: int, limit: int
) -> list[dict[str, Any]]:
    """Embedded index entries for ``model`` after ``after_rowid``: their text and stored hash."""
    sql = (
        f"SELECT {SEARCH_INDEX}.rowid AS rowid, {SEARCH_INDEX}.kind AS kind, "
        f"{SEARCH_INDEX}.ref_id AS ref_id, {SEARCH_INDEX}.title AS title, "
        f"{SEARCH_INDEX}.body AS body, embeddings.text_hash AS text_hash "
        f"FROM {SEARCH_INDEX} CROSS JOIN embeddings ON {_EMBED_JOIN} "
        f"WHERE {SEARCH_INDEX}.rowid > :after ORDER BY {SEARCH_INDEX}.rowid ASC LIMIT :limit"
    )
    stmt = text(sql).bindparams(model=model, after=after_rowid, limit=limit)
    return [dict(row) for row in await fetch_all(conn, stmt)]


async def upsert_embeddings(conn: aiosqlite.Connection, rows: list[dict[str, Any]]) -> None:
    """Insert or replace vectors keyed by ``(kind, ref_id, model)``."""
    if not rows:
        return
    stmt = sqlite_insert(embeddings).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[embeddings.c.kind, embeddings.c.ref_id, embeddings.c.model],
        set_={
            "dim": stmt.excluded.dim,
            "vector": stmt.excluded.vector,
            "text_hash": stmt.excluded.text_hash,
            "created_at": stmt.excluded.created_at,
        },
    )
    await run(conn, stmt)


async def embed_counts(conn: aiosqlite.Connection, model: str, dim: int | None) -> dict[str, int]:
    """How many index entries have a usable vector for ``model`` and how many wait for one."""
    params: dict[str, Any] = {"model": model}
    on = _EMBED_JOIN + _dim_clause(dim, params)
    sql = (
        "SELECT count(embeddings.ref_id) AS indexed, "
        "sum(CASE WHEN embeddings.ref_id IS NULL THEN 1 ELSE 0 END) AS pending "
        f"FROM {SEARCH_INDEX} LEFT JOIN embeddings ON {on}"
    )
    row = await fetch_one(conn, _bind(sql, params))
    if row is None:
        return {"indexed": 0, "pending": 0}
    return {"indexed": int(row["indexed"] or 0), "pending": int(row["pending"] or 0)}


async def vector_candidates(
    conn: aiosqlite.Connection,
    model: str,
    dim: int,
    *,
    kinds: Iterable[str] | None,
    channel_id: int | None,
    author: str | None,
    since: str | None,
):
    """Yield every embedded entry of ``model`` / ``dim`` that passes the BM25 filters.

    Rows stream from the cursor so the caller can keep a bounded top-k heap.
    """
    where, params = _search_where(
        kinds=kinds, channel_id=channel_id, author=author, since=since, match=False
    )
    params["model"] = model
    params["dim"] = dim
    sql = (
        f"SELECT {SEARCH_INDEX}.kind AS kind, {SEARCH_INDEX}.ref_id AS ref_id, "
        f"{SEARCH_INDEX}.channel_id AS channel_id, channels.name AS channel, "
        f"{SEARCH_INDEX}.author AS author, {SEARCH_INDEX}.created_at AS created_at, "
        f"{SEARCH_INDEX}.title AS title, {SEARCH_INDEX}.body AS body, "
        "embeddings.vector AS vector "
        f"FROM {SEARCH_INDEX} CROSS JOIN embeddings ON {_EMBED_JOIN} AND embeddings.dim = :dim "
        f"LEFT JOIN channels ON channels.id = {SEARCH_INDEX}.channel_id WHERE {where}"
    )
    cursor = await run(conn, _bind(sql, params))
    async for row in cursor:
        yield row


async def list_agents(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Observed handles and their latest timestamp. Not a presence list."""
    seen = union_all(
        select(messages.c.author.label("name"), messages.c.created_at.label("seen_at")),
        select(tasks.c.assignee.label("name"), tasks.c.updated_at.label("seen_at")).where(
            tasks.c.assignee.is_not(None)
        ),
        select(task_events.c.actor.label("name"), task_events.c.created_at.label("seen_at")),
    ).subquery("seen")
    last_activity = func.max(seen.c.seen_at).label("last_activity")
    rows = await fetch_all(
        conn,
        select(seen.c.name, last_activity)
        .group_by(seen.c.name)
        .order_by(last_activity.desc(), seen.c.name.asc()),
    )
    return [{"name": row["name"], "last_activity": row["last_activity"]} for row in rows]
