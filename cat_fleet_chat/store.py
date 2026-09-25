"""All SQL for channels, messages, mentions, tasks, attachments, and the event stream.

Statements are built with SQLAlchemy Core (see ``schema.py``) and run on the
caller's aiosqlite connection. Callers own the transaction. This module does
not commit, notify waiters, or enforce business rules.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import aiosqlite
from sqlalchemy import func, insert, literal, select, union_all, update

from cat_fleet_chat.schema import (
    attachments,
    channels,
    events,
    fetch_all,
    fetch_one,
    idempotency,
    mentions,
    messages,
    run,
    task_events,
    tasks,
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
    ).join_from(source, channels, channels.c.id == source.c.channel_id)


def _message_row(
    row: aiosqlite.Row, mention_names: list[str], files: list[dict[str, Any]]
) -> dict[str, Any]:
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
) -> int:
    cursor = await run(
        conn,
        insert(messages).values(
            channel_id=channel_id,
            author=author,
            text=text,
            reply_to=reply_to,
            created_at=created_at,
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
