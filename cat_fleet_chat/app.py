"""One Starlette app: REST, MCP, and the portal, on one port and one lifespan."""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount

from cat_fleet_chat.config import Settings
from cat_fleet_chat.db import Database, ProcessLock
from cat_fleet_chat.embed import Embedder, EmbedQueue, build_embedder
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.mcp_app import build_mcp, set_dispatcher, set_hub
from cat_fleet_chat.rest_app import Guard, api_routes, hook_routes
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.web_app import spa_routes
from cat_fleet_chat.webhooks import WebhookDispatcher

logger = logging.getLogger("cat_fleet_chat.app")


def create_app(
    settings: Settings,
    *,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
    webhook_interval: float = 2.0,
    embedder: Embedder | None = None,
) -> Starlette:
    """Build the hub app. The process lock and database open during lifespan.

    ``webhook_transport`` and ``webhook_interval`` exist for tests: they swap the
    outbound HTTP transport and the per-hook pacing. ``embedder`` replaces the
    one the settings describe (tests pass a fake).
    """
    mcp = build_mcp()
    # path="/" because the parent mounts this app at /mcp. Stateless mode
    # matches short-lived MCP clients that do not keep a session.
    mcp_http = mcp.http_app(path="/", stateless_http=True, host_origin_protection=False)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        lock = ProcessLock(settings.db_path)
        lock.acquire()
        db = Database(settings.db_path)
        await db.open()
        hub = Hub(db, WaitHub())
        # Feed posts starting with "LEARNED:" become knowledge facts.
        hub.feed_post_hooks.append(hub.knowledge_from_feed_post)
        app.state.hub = hub
        app.state.settings = settings
        app.state.process_lock = lock
        set_hub(hub)
        dispatcher: WebhookDispatcher | None = None
        if settings.webhooks_enabled:
            dispatcher = WebhookDispatcher(
                hub, transport=webhook_transport, min_interval=webhook_interval
            )
            dispatcher.start()
            hub.discord_resolver = dispatcher.discord_info
        app.state.webhooks = dispatcher
        set_dispatcher(dispatcher)
        # Embeddings are optional: provider "off" keeps a NullEmbedder and the
        # queue never starts. The queue runs beside the hub and never blocks writes.
        active_embedder = embedder if embedder is not None else build_embedder(settings)
        queue = EmbedQueue(db, active_embedder, batch=settings.embed_batch)
        hub.embedder = active_embedder
        hub.embed_queue = queue
        queue.start()
        app.state.embed_queue = queue
        try:
            async with mcp_http.lifespan(app):
                yield
        finally:
            # Every step runs even if an earlier one raises; the first error is re-raised last.
            errors: list[BaseException] = []

            async def step(action):
                try:
                    result = action()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001 — keep releasing the rest
                    logger.exception("shutdown step failed")
                    errors.append(exc)

            def detach_embed() -> None:
                hub.embed_queue = None

            def detach_dispatcher() -> None:
                set_dispatcher(None)
                hub.discord_resolver = None

            await step(queue.stop)
            await step(detach_embed)
            if embedder is None:
                await step(active_embedder.aclose)
            await step(detach_dispatcher)
            if dispatcher is not None:
                await step(dispatcher.stop)
            await step(lambda: set_hub(None))
            await step(db.close)
            await step(lock.release)
            if errors:
                raise errors[0]

    app = Starlette(
        routes=[
            *api_routes(),
            *hook_routes(),
            Mount("/mcp", app=mcp_http),
            *spa_routes(),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(Guard, settings=settings)
    return app
