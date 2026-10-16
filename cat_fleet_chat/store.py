"""All SQL for channels, messages, mentions, tasks, attachments, the feed, and the event stream.

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
from sqlalchemy import delete, func, insert, literal, select, union_all, update

from cat_fleet_chat.schema import (
    attachments,
    channels,
    events,
    feed_follows,
    feed_hashtags,
    feed_posts,
    fetch_all,
    fetch_one,
    idempotency,
    mentions,
    messages,
    run,
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


# ---- feed --------------------------------------------------------------

FEED_COLUMNS = (
    feed_posts.c.id,
    feed_posts.c.author,
    feed_posts.c.body,
    feed_posts.c.parent_id,
    feed_posts.c.root_id,
    feed_posts.c.repost_of,
    feed_posts.c.quote_of,
    feed_posts.c.depth,
    feed_posts.c.created_at,
    feed_posts.c.deleted_at,
)


async def insert_feed_post(
    conn: aiosqlite.Connection,
    *,
    author: str,
    body: str,
    parent_id: int | None,
    root_id: int | None,
    repost_of: int | None,
    quote_of: int | None,
    depth: int,
    created_at: str,
    tags: Iterable[str] = (),
) -> int:
    """Insert one post and its hashtags. A root (``root_id=None``) points at itself."""
    cursor = await run(
        conn,
        insert(feed_posts).values(
            author=author,
            body=body,
            parent_id=parent_id,
            root_id=root_id,
            repost_of=repost_of,
            quote_of=quote_of,
            depth=depth,
            created_at=created_at,
        ),
    )
    post_id = int(cursor.lastrowid)
    if root_id is None:
        await run(conn, update(feed_posts).where(feed_posts.c.id == post_id).values(root_id=post_id))
    for tag in tags:
        await run(conn, insert(feed_hashtags).values(post_id=post_id, tag=tag))
    return post_id


async def get_feed_row(conn: aiosqlite.Connection, post_id: int) -> dict[str, Any] | None:
    row = await fetch_one(conn, select(*FEED_COLUMNS).where(feed_posts.c.id == post_id))
    return dict(row) if row else None


async def feed_rows(conn: aiosqlite.Connection, post_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted(set(post_ids))
    if not ids:
        return {}
    rows = await fetch_all(conn, select(*FEED_COLUMNS).where(feed_posts.c.id.in_(ids)))
    return {row["id"]: dict(row) for row in rows}


async def find_repost(
    conn: aiosqlite.Connection, author: str, original_id: int
) -> dict[str, Any] | None:
    row = await fetch_one(
        conn,
        select(*FEED_COLUMNS).where(
            feed_posts.c.author == author, feed_posts.c.repost_of == original_id
        ),
    )
    return dict(row) if row else None


async def set_feed_deleted(conn: aiosqlite.Connection, post_id: int, deleted_at: str | None) -> None:
    await run(conn, update(feed_posts).where(feed_posts.c.id == post_id).values(deleted_at=deleted_at))


async def feed_counts(
    conn: aiosqlite.Connection, post_ids: Iterable[int]
) -> dict[int, dict[str, int]]:
    """Live reply, repost and quote counts per post. Deleted posts do not count."""
    ids = sorted(set(post_ids))
    found: dict[int, dict[str, int]] = {
        post_id: {"reply_count": 0, "repost_count": 0, "quote_count": 0} for post_id in ids
    }
    if not ids:
        return found
    for column, name in (
        (feed_posts.c.parent_id, "reply_count"),
        (feed_posts.c.repost_of, "repost_count"),
        (feed_posts.c.quote_of, "quote_count"),
    ):
        rows = await fetch_all(
            conn,
            select(column.label("target"), func.count().label("n"))
            .where(column.in_(ids), feed_posts.c.deleted_at.is_(None))
            .group_by(column),
        )
        for row in rows:
            found[row["target"]][name] = int(row["n"])
    return found


async def feed_tags_for(
    conn: aiosqlite.Connection, post_ids: Iterable[int]
) -> dict[int, list[str]]:
    ids = sorted(set(post_ids))
    if not ids:
        return {}
    rows = await fetch_all(
        conn,
        select(feed_hashtags.c.post_id, feed_hashtags.c.tag)
        .where(feed_hashtags.c.post_id.in_(ids))
        .order_by(feed_hashtags.c.post_id, feed_hashtags.c.tag),
    )
    found: dict[int, list[str]] = {}
    for row in rows:
        found.setdefault(row["post_id"], []).append(row["tag"])
    return found


async def feed_timeline_rows(
    conn: aiosqlite.Connection,
    *,
    before_id: int | None,
    limit: int,
    author: str | None = None,
    follower: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """Live posts newest first, at most ``limit``. Filters combine with AND."""
    stmt = select(*FEED_COLUMNS).where(feed_posts.c.deleted_at.is_(None))
    if before_id is not None:
        stmt = stmt.where(feed_posts.c.id < before_id)
    if author is not None:
        stmt = stmt.where(feed_posts.c.author == author)
    if follower is not None:
        followed = select(feed_follows.c.followee).where(feed_follows.c.follower == follower)
        stmt = stmt.where(feed_posts.c.author.in_(followed.scalar_subquery()))
    if tag is not None:
        tagged = select(feed_hashtags.c.post_id).where(feed_hashtags.c.tag == tag)
        stmt = stmt.where(feed_posts.c.id.in_(tagged.scalar_subquery()))
    rows = await fetch_all(conn, stmt.order_by(feed_posts.c.id.desc()).limit(limit))
    return [dict(row) for row in rows]


async def feed_ancestor_rows(conn: aiosqlite.Connection, post_id: int) -> list[dict[str, Any]]:
    """The post and every ancestor up to its root, root first (index ``i`` has depth ``i``)."""
    chain = select(feed_posts.c.id, feed_posts.c.parent_id).where(feed_posts.c.id == post_id)
    chain = chain.cte("chain", recursive=True)
    chain = chain.union_all(
        select(feed_posts.c.id, feed_posts.c.parent_id).where(feed_posts.c.id == chain.c.parent_id)
    )
    rows = await fetch_all(
        conn,
        select(*FEED_COLUMNS)
        .where(feed_posts.c.id.in_(select(chain.c.id)))
        .order_by(feed_posts.c.depth.asc()),
    )
    return [dict(row) for row in rows]


async def feed_subtree_rows(
    conn: aiosqlite.Connection,
    top_id: int,
    *,
    max_depth: int,
    after_id: int | None,
    limit: int,
    exclude: Iterable[int] = (),
) -> list[dict[str, Any]]:
    """``top_id`` and its replies down to ``max_depth``, deleted ones included.

    Oldest first, ids above ``after_id``, at most ``limit``, without ``exclude``.
    """
    sub = select(feed_posts.c.id).where(feed_posts.c.id == top_id).cte("sub", recursive=True)
    sub = sub.union_all(
        select(feed_posts.c.id).where(
            feed_posts.c.parent_id == sub.c.id, feed_posts.c.depth <= max_depth
        )
    )
    stmt = select(*FEED_COLUMNS).where(feed_posts.c.id.in_(select(sub.c.id)))
    if after_id is not None:
        stmt = stmt.where(feed_posts.c.id > after_id)
    skip = sorted(set(exclude))
    if skip:
        stmt = stmt.where(feed_posts.c.id.not_in(skip))
    rows = await fetch_all(conn, stmt.order_by(feed_posts.c.id.asc()).limit(limit))
    return [dict(row) for row in rows]


async def feed_ids_with_replies(conn: aiosqlite.Connection, post_ids: Iterable[int]) -> set[int]:
    """Which of ``post_ids`` have at least one reply, deleted replies included."""
    ids = sorted(set(post_ids))
    if not ids:
        return set()
    rows = await fetch_all(
        conn, select(feed_posts.c.parent_id).where(feed_posts.c.parent_id.in_(ids)).distinct()
    )
    return {int(row["parent_id"]) for row in rows}


async def insert_follow(
    conn: aiosqlite.Connection, follower: str, followee: str, created_at: str
) -> bool:
    """Whether a new follow row was written (``False`` when it already existed)."""
    cursor = await run(
        conn,
        insert(feed_follows)
        .prefix_with("OR IGNORE")
        .values(follower=follower, followee=followee, created_at=created_at),
    )
    return cursor.rowcount == 1


async def delete_follow(conn: aiosqlite.Connection, follower: str, followee: str) -> bool:
    cursor = await run(
        conn,
        delete(feed_follows).where(
            feed_follows.c.follower == follower, feed_follows.c.followee == followee
        ),
    )
    return cursor.rowcount == 1


async def list_follows(
    conn: aiosqlite.Connection, *, follower: str | None = None, followee: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(feed_follows.c.follower, feed_follows.c.followee, feed_follows.c.created_at)
    if follower is not None:
        stmt = stmt.where(feed_follows.c.follower == follower).order_by(feed_follows.c.followee)
    if followee is not None:
        stmt = stmt.where(feed_follows.c.followee == followee).order_by(feed_follows.c.follower)
    return [dict(row) for row in await fetch_all(conn, stmt)]


async def trending_tags(
    conn: aiosqlite.Connection, *, since: str, limit: int
) -> list[dict[str, Any]]:
    """Tags by number of live posts created at or after ``since``."""
    uses = func.count().label("count")
    rows = await fetch_all(
        conn,
        select(feed_hashtags.c.tag, uses)
        .join_from(feed_hashtags, feed_posts, feed_posts.c.id == feed_hashtags.c.post_id)
        .where(feed_posts.c.created_at >= since, feed_posts.c.deleted_at.is_(None))
        .group_by(feed_hashtags.c.tag)
        .order_by(uses.desc(), feed_hashtags.c.tag.asc())
        .limit(limit),
    )
    return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
