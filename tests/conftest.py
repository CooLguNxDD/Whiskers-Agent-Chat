"""App fixture. Each test gets its own database and process lock.

The FastMCP lifespan uses an anyio cancel scope, which must be exited from
the same task that entered it. The hub therefore stays open inside ``serve``
for the whole test, and that task closes it.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from cat_fleet_chat.app import create_app
from cat_fleet_chat.config import Settings


async def _serve(app, started: asyncio.Event, stop: asyncio.Event) -> None:
    try:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()
    finally:
        started.set()


async def _open(settings: Settings):
    app = create_app(settings)
    started = asyncio.Event()
    stop = asyncio.Event()
    task = asyncio.create_task(_serve(app, started, stop))
    await started.wait()
    if task.done():
        task.result()
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://testserver")
    await client.__aenter__()
    try:
        yield client, app
    finally:
        await client.__aexit__(None, None, None)
        stop.set()
        await task


@pytest.fixture
async def running(tmp_path):
    settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token=None,
        dev_origins=("http://127.0.0.1:3001", "http://localhost:3001"),
    )
    async for item in _open(settings):
        yield item


@pytest.fixture
async def authed(tmp_path):
    settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token="test-token",
        dev_origins=("http://127.0.0.1:3001",),
    )
    async for item in _open(settings):
        yield item
