"""SQLite connections, migrations, and the single-process lock.

One hub process owns a database. Writes run on a single connection behind an
asyncio lock. Reads use a small pool so a waiter can query without taking the
writer lock. Every connection enables foreign keys, WAL, and a busy timeout.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiosqlite
from sqlalchemy import func, insert, select, update

from cat_fleet_chat import schema

logger = logging.getLogger("cat_fleet_chat.db")

SCHEMA_VERSION = 5

READ_POOL_SIZE = 4


class ProcessLock:
    """Exclusive lockfile so a second hub against the same database fails fast.

    In-memory waiters cannot cross processes. The lock is held until
    :meth:`release`.
    """

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        self.path = path.with_name(path.name + ".lock")
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 — held for the process lifetime
        try:
            if self.path.stat().st_size < 1:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            _lock_file(fh)
        except OSError as exc:
            fh.close()
            raise SystemExit(
                f"Another cat-fleet-chat process already holds {self.path}. "
                "V1 runs one hub process per database. Stop the other process "
                f"or choose a different CAT_FLEET_DB_PATH. ({exc})"
            ) from exc
        self._fh = fh
        logger.info("process lock acquired on %s", self.path)

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            _unlock_file(fh)
        except OSError:
            logger.debug("process lock unlock failed", exc_info=True)
        fh.close()


def _lock_file(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class Database:
    """Serialized writer plus a bounded read pool."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._write: aiosqlite.Connection | None = None
        self._reads: list[aiosqlite.Connection] = []
        self._read_q: asyncio.Queue[aiosqlite.Connection] | None = None

    async def open(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write = await self._connect()
        await self._migrate(self._write)
        self._read_q = asyncio.Queue()
        for _ in range(READ_POOL_SIZE):
            conn = await self._connect()
            self._reads.append(conn)
            self._read_q.put_nowait(conn)

    async def close(self) -> None:
        for conn in self._reads:
            await conn.close()
        self._reads.clear()
        self._read_q = None
        if self._write is not None:
            await self._write.close()
            self._write = None

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """Create missing tables and columns from ``schema.metadata``. Safe to repeat."""
        for statement in schema.ddl_statements():
            await conn.execute(statement)
        for table, name in schema.ADDED_COLUMNS:
            info = func.pragma_table_info(table.name).table_valued("name")
            present = {row["name"] for row in await schema.fetch_all(conn, select(info.c.name))}
            if name not in present:
                await conn.execute(schema.add_column_sql(table, name))
        # v3 backfill: a channel archived under v2 gets the matching state.
        await schema.run(
            conn,
            update(schema.channels)
            .where(
                schema.channels.c.archived_at.is_not(None),
                schema.channels.c.state != "archived",
            )
            .values(state="archived"),
        )
        now = func.datetime("now")
        for version in range(1, SCHEMA_VERSION + 1):
            await schema.run(
                conn,
                insert(schema.schema_migrations)
                .prefix_with("OR IGNORE")
                .values(version=version, applied_at=now),
            )
        # Idempotent bootstrap. A database created before the seed, or one
        # whose fleet row was never inserted, still gets exactly one fleet.
        await schema.run(
            conn,
            insert(schema.channels)
            .prefix_with("OR IGNORE")
            .values(name="fleet", topic="Fleet-wide chat", created_at=now),
        )
        await conn.commit()
        logger.info("schema ready at %s", self.db_path)

    async def write(self, fn):
        """Run ``fn(conn)`` as the only writer. ``fn`` owns its transaction."""
        if self._write is None:
            raise RuntimeError("database is not open")
        async with self._write_lock:
            return await fn(self._write)

    async def read(self, fn):
        """Run ``fn(conn)`` inside a read transaction on a pooled connection."""
        if self._read_q is None:
            raise RuntimeError("database is not open")
        conn = await self._read_q.get()
        try:
            await conn.execute("BEGIN")
            try:
                return await fn(conn)
            finally:
                await conn.rollback()
        finally:
            self._read_q.put_nowait(conn)


def foreign_keys_enabled_sql() -> str:
    """Statement tests use to prove the pragma is on."""
    return "PRAGMA foreign_keys"
