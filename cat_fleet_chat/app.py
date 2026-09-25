"""One Starlette app: REST, MCP, and the portal, on one port and one lifespan."""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Mount

from cat_fleet_chat.config import Settings
from cat_fleet_chat.db import Database, ProcessLock
from cat_fleet_chat.hub import Hub
from cat_fleet_chat.mcp_app import build_mcp, set_hub
from cat_fleet_chat.rest_app import Guard, api_routes
from cat_fleet_chat.waiters import WaitHub
from cat_fleet_chat.web_app import spa_routes


def create_app(settings: Settings) -> Starlette:
    """Build the hub app. The process lock and database open during lifespan."""
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
        app.state.hub = hub
        app.state.settings = settings
        app.state.process_lock = lock
        set_hub(hub)
        try:
            async with mcp_http.lifespan(app):
                yield
        finally:
            set_hub(None)
            await db.close()
            lock.release()

    app = Starlette(
        routes=[
            *api_routes(),
            Mount("/mcp", app=mcp_http),
            *spa_routes(),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(Guard, settings=settings)
    return app
