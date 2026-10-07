"""App fixture. Each test gets its own database and process lock.

The FastMCP lifespan uses an anyio cancel scope, which must be exited from
the same task that entered it. The hub therefore stays open inside ``serve``
for the whole test, and that task closes it.
"""

from __future__ import annotations

import asyncio
import json

import httpx
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


async def _open(settings: Settings, **app_kwargs):
    app = create_app(settings, **app_kwargs)
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
    # The outbound dispatcher is a permanent waiter on the wake condition, which
    # would skew the waiter-count assertions in test_hub. Webhook tests use
    # ``hooked`` below instead.
    settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token=None,
        dev_origins=("http://127.0.0.1:3001", "http://localhost:3001"),
        webhooks_enabled=False,
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
        webhooks_enabled=False,
    )
    async for item in _open(settings):
        yield item


class Sink:
    """Stands in for the outside world: records outbound webhook requests.

    ``replies`` is consumed one status per request; once empty, ``default``
    answers. A reply may be an int or a ``(status, headers)`` pair.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: list = []
        self.default = 204
        self.error_text = "nope"
        # Raised instead of answering, to simulate a network failure.
        self.fail_with: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        reply = self.replies.pop(0) if self.replies else self.default
        status, headers = reply if isinstance(reply, tuple) else (reply, {})
        return httpx.Response(status, headers=headers, text="" if status < 400 else self.error_text)

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
async def hooked(tmp_path):
    """App with the outbound dispatcher on, talking to a recording ``Sink``. Yields (client, app, sink)."""
    sink = Sink()
    settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token="test-token",
        dev_origins=("http://127.0.0.1:3001",),
    )
    async for client, app in _open(
        settings,
        webhook_transport=httpx.MockTransport(sink.handler),
        webhook_interval=0,
    ):
        client.headers["Authorization"] = "Bearer test-token"
        yield client, app, sink
