"""Table metadata and the bridge from SQLAlchemy Core statements to aiosqlite.

SQLAlchemy is the query builder only. Statements compile for SQLite with
``qmark`` parameters and run on the hub's own aiosqlite connections, so the
single-writer lock and ``BEGIN IMMEDIATE`` transactions in ``db.py`` stay the
one place that owns connections and commits.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.sql import ClauseElement

metadata = MetaData()

schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", Text, nullable=False),
)

channels = Table(
    "channels",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("topic", Text, nullable=False, server_default=text("''")),
    Column("created_at", Text, nullable=False),
    # Added in schema v2. A non-null archived_at makes the channel read-only.
    Column("archived_at", Text),
    Column("archived_by", Text),
    # Added in schema v3. Lifecycle label (validate.CHANNEL_STATES). "archived"
    # always agrees with archived_at; the hub changes both in one statement.
    Column("state", Text, nullable=False, server_default=text("'active'")),
    Column("state_note", Text, nullable=False, server_default=text("''")),
    Column("state_updated_at", Text),
    Column("state_updated_by", Text),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("channel_id", Integer, ForeignKey("channels.id"), nullable=False),
    Column("author", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("reply_to", Integer, ForeignKey("messages.id")),
    Column("created_at", Text, nullable=False),
    Index("idx_messages_channel_id", "channel_id", "id"),
)

mentions = Table(
    "mentions",
    metadata,
    Column("message_id", Integer, ForeignKey("messages.id"), nullable=False),
    Column("agent_name", Text, nullable=False),
    PrimaryKeyConstraint("message_id", "agent_name"),
    Index("idx_mentions_agent", "agent_name", "message_id"),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("channel_id", Integer, ForeignKey("channels.id"), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("status", Text, nullable=False),
    Column("assignee", Text),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_at", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)

task_events = Table(
    "task_events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("task_id", Integer, ForeignKey("tasks.id"), nullable=False),
    Column("actor", Text, nullable=False),
    Column("from_status", Text),
    Column("to_status", Text, nullable=False),
    Column("note", Text, nullable=False, server_default=text("''")),
    Column("created_at", Text, nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("channel_id", Integer),
    Column("entity_id", Integer, nullable=False),
    Column("payload_json", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Index("idx_events_channel_id", "channel_id", "id"),
)

idempotency = Table(
    "idempotency",
    metadata,
    Column("client_request_id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("fingerprint", Text, nullable=False),
    Column("result_json", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

# Schema v2. Metadata only: the bytes live in an external object store (the
# Whiskers MinIO stack), addressed by bucket + object key.
attachments = Table(
    "attachments",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("message_id", Integer, ForeignKey("messages.id"), nullable=False),
    Column("filename", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("storage", Text, nullable=False),
    Column("bucket", Text, nullable=False),
    Column("object_key", Text, nullable=False),
    Column("sha256", Text, nullable=False, server_default=text("''")),
    Column("created_at", Text, nullable=False),
    Index("idx_attachments_message", "message_id", "id"),
)

# Columns that a database created at an older version is missing. SQLite has
# no "ADD COLUMN IF NOT EXISTS", so db.py checks table_info before adding.
ADDED_COLUMNS: tuple[tuple[Table, str], ...] = (
    (channels, "archived_at"),
    (channels, "archived_by"),
    (channels, "state"),
    (channels, "state_note"),
    (channels, "state_updated_at"),
    (channels, "state_updated_by"),
)

DIALECT = sqlite.dialect(paramstyle="qmark")


def compile_sql(stmt: ClauseElement) -> tuple[str, list[Any]]:
    """Compile ``stmt`` to SQLite text plus positional parameters.

    ``render_postcompile`` expands ``IN`` lists so every bound value has a
    plain ``?`` placeholder.
    """
    compiled = stmt.compile(dialect=DIALECT, compile_kwargs={"render_postcompile": True})
    params = [compiled.params[name] for name in (compiled.positiontup or ())]
    return str(compiled), params


async def run(conn: aiosqlite.Connection, stmt: ClauseElement) -> aiosqlite.Cursor:
    """Execute a built statement on an aiosqlite connection."""
    sql, params = compile_sql(stmt)
    return await conn.execute(sql, params)


async def fetch_all(conn: aiosqlite.Connection, stmt: ClauseElement) -> list[aiosqlite.Row]:
    cursor = await run(conn, stmt)
    return list(await cursor.fetchall())


async def fetch_one(conn: aiosqlite.Connection, stmt: ClauseElement) -> aiosqlite.Row | None:
    cursor = await run(conn, stmt)
    return await cursor.fetchone()


def ddl_statements() -> list[str]:
    """``CREATE TABLE/INDEX IF NOT EXISTS`` for every table, in dependency order."""
    out: list[str] = []
    for table in metadata.sorted_tables:
        out.append(str(CreateTable(table, if_not_exists=True).compile(dialect=DIALECT)))
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            out.append(str(CreateIndex(index, if_not_exists=True).compile(dialect=DIALECT)))
    return out


def add_column_sql(table: Table, name: str) -> str:
    """``ALTER TABLE ... ADD COLUMN`` for one metadata column.

    Core has no ALTER builder (that is Alembic's job), so the column name and
    type still come from metadata rather than hand-written strings.
    """
    column = table.c[name]
    sql = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.type.compile(dialect=DIALECT)}"
    # SQLite only accepts ADD COLUMN ... NOT NULL together with a default.
    if column.server_default is not None:
        default = column.server_default.arg.compile(dialect=DIALECT)
        if not column.nullable:
            sql += " NOT NULL"
        sql += f" DEFAULT {default}"
    return sql
