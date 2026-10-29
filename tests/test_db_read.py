"""Pooled reads survive cancellation: a dropped SSE stream must not poison a read connection."""

from __future__ import annotations

import asyncio

import anyio

from cat_fleet_chat.db import READ_POOL_SIZE, Database


async def _count(conn) -> int:
    cursor = await conn.execute("SELECT COUNT(*) FROM channels")
    return (await cursor.fetchone())[0]


async def test_read_survives_level_triggered_cancellation(tmp_path):
    """anyio (Starlette's StreamingResponse) re-cancels at every await, including the rollback."""
    db = Database(str(tmp_path / "hub.sqlite"))
    await db.open()
    try:

        async def slow(conn):
            await conn.execute("SELECT 1")
            await asyncio.sleep(10)

        for _ in range(READ_POOL_SIZE):
            with anyio.move_on_after(0.05):
                await db.read(slow)
        for _ in range(READ_POOL_SIZE * 3):
            assert await db.read(_count) == 1
    finally:
        await db.close()


async def test_read_heals_a_connection_left_in_a_transaction(tmp_path):
    db = Database(str(tmp_path / "hub.sqlite"))
    await db.open()
    try:
        poisoned = await db._read_q.get()
        await poisoned.execute("BEGIN")
        db._read_q.put_nowait(poisoned)
        for _ in range(READ_POOL_SIZE * 2):
            assert await db.read(_count) == 1
    finally:
        await db.close()
