"""JSON and SSE routes. The plugin and the React portal both use this surface."""

from __future__ import annotations

import asyncio
import hmac
import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from cat_fleet_chat.config import Settings, allowed_hostnames, allowed_origins
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub, SSE_BATCH, agent_relevant
from cat_fleet_chat.validate import channel_name, event_kinds, handle, optional_id
from cat_fleet_chat.webhooks import sign

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hub(request: Request) -> Hub:
    return request.app.state.hub


def _hostname(host_header: str) -> str:
    text = host_header.strip()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end].lower() if end > 1 else text.lower()
    return text.split(":", 1)[0].lower()


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "unauthorized", "message": "missing or invalid bearer token"}},
        status_code=401,
    )


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "forbidden", "message": message}},
        status_code=403,
    )


class Guard:
    """Bearer auth plus browser Origin/Host checks.

    Every caller must use an allowed Host, even when Origin is absent.
    A browser write must come from an allowed origin, and its Host must be
    one this process expects, so a DNS-rebinding page cannot ride the open
    loopback mode into a write.
    """

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self._origins = allowed_origins(settings)
        self._hosts = allowed_hostnames(settings)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/") or path.startswith("/mcp"):
            request = Request(scope)
            denied = self._denied(request)
            if denied is not None:
                await denied(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _denied(self, request: Request) -> JSONResponse | None:
        if self.settings.token:
            presented = request.headers.get("authorization", "")
            expected = f"Bearer {self.settings.token}"
            if not hmac.compare_digest(presented, expected):
                return _unauthorized()

        origin = request.headers.get("origin")
        method = request.method.upper()
        if method in _WRITE_METHODS:
            if origin is not None and origin.rstrip("/") not in self._origins:
                return _forbidden("cross-origin write rejected")
            if request.headers.get("sec-fetch-site") == "cross-site":
                return _forbidden("cross-site write rejected")

        # Same-origin browser GETs need not send Origin (including DNS rebinding).
        host = _hostname(request.headers.get("host", ""))
        if host not in self._hosts:
            return JSONResponse(
                {"error": {"code": "invalid_host", "message": "host is not allowed"}},
                status_code=421,
            )
        return None


def _json_error(exc: HubError) -> JSONResponse:
    return JSONResponse(exc.rest_body(), status_code=exc.status)


async def _body(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith("application/json"):
        raw = await request.body()
        if not raw:
            return {}
    try:
        parsed = await request.json()
    except Exception as exc:
        raise HubError("validation_error", "request body must be JSON", 400) from exc
    if not isinstance(parsed, dict):
        raise HubError("validation_error", "request body must be a JSON object", 400)
    return parsed


def _author(body: dict[str, Any], field: str = "author") -> Any:
    if body.get(field) not in (None, ""):
        return body.get(field)
    return body.get("agent_name")


async def list_channels(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        return JSONResponse(
            await _hub(request).list_channels(params.get("include_archived"), params.get("state"))
        )
    except HubError as exc:
        return _json_error(exc)


async def create_channel(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).create_channel(body.get("name"), body.get("topic"))
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def list_messages(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).get_messages(
            params.get("channel"),
            since_id=params.get("since_id"),
            before_id=params.get("before_id"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def post_message(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).post_message(
            body.get("channel"),
            _author(body),
            body.get("text"),
            reply_to=body.get("reply_to"),
            client_request_id_value=body.get("client_request_id"),
            attachments_value=body.get("attachments"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def archive_channel(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).archive_channel(
            request.path_params["name"],
            _author(body, "actor"),
            note=body.get("note"),
            force=body.get("force", False),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def unarchive_channel(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).unarchive_channel(
            request.path_params["name"], _author(body, "actor"), body.get("state") or "active"
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def set_channel_state(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).set_channel_state(
            request.path_params["name"],
            body.get("state"),
            _author(body, "actor"),
            note=body.get("note"),
            force=body.get("force", False),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def get_attachment(request: Request) -> JSONResponse:
    try:
        return JSONResponse(
            await _hub(request).get_attachment(request.path_params["attachment_id"])
        )
    except HubError as exc:
        return _json_error(exc)


async def notifications(request: Request) -> JSONResponse:
    """Long-poll for events relevant to ``agent``. ``timeout=0`` returns at once."""
    try:
        params = request.query_params
        result = await _hub(request).wait_for_events(
            params.get("agent"),
            params.get("after_event_id", 0),
            kinds=params.get("kinds"),
            channel=params.get("channel"),
            timeout=params.get("timeout"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)
    except asyncio.CancelledError:
        raise


async def event_cursor(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).event_cursor())
    except HubError as exc:
        return _json_error(exc)


async def wait_for_mentions(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).wait_for_mentions(
            params.get("agent"),
            params.get("since_id", 0),
            channel=params.get("channel"),
            timeout=params.get("timeout"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)
    except asyncio.CancelledError:
        raise


async def list_tasks(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).list_tasks(
            channel=params.get("channel"),
            status=params.get("status"),
            assignee=params.get("assignee"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def create_task(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).create_task(
            body.get("channel"),
            body.get("title"),
            description=body.get("description"),
            assignee=body.get("assignee"),
            actor=_author(body, "actor"),
            client_request_id_value=body.get("client_request_id"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def claim_task(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).claim_task(
            request.path_params["task_id"],
            _author(body, "actor"),
            expected_version=body.get("expected_version"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def update_task_status(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).update_task_status(
            request.path_params["task_id"],
            body.get("status"),
            _author(body, "actor"),
            body.get("expected_version"),
            note=body.get("note"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def list_agents(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).list_agents())
    except HubError as exc:
        return _json_error(exc)


async def list_webhooks(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).list_webhooks(request.query_params.get("direction")))
    except HubError as exc:
        return _json_error(exc)


async def create_webhook(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).create_webhook(
            body.get("name"),
            body.get("direction"),
            url=body.get("url"),
            format=body.get("format"),
            kinds=body.get("kinds"),
            channels=body.get("channels"),
            mentions=body.get("mentions"),
            exclude_authors=body.get("exclude_authors"),
            channel=body.get("channel"),
            author=body.get("author"),
            allow_override=body.get("allow_override", False),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def get_webhook(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).get_webhook(request.path_params["hook_id"]))
    except HubError as exc:
        return _json_error(exc)


async def update_webhook(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        return JSONResponse(await _hub(request).update_webhook(request.path_params["hook_id"], body))
    except HubError as exc:
        return _json_error(exc)


async def delete_webhook(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).delete_webhook(request.path_params["hook_id"]))
    except HubError as exc:
        return _json_error(exc)


async def rotate_webhook_secret(request: Request) -> JSONResponse:
    try:
        return JSONResponse(
            await _hub(request).rotate_webhook_secret(request.path_params["hook_id"])
        )
    except HubError as exc:
        return _json_error(exc)


async def test_webhook(request: Request) -> JSONResponse:
    """Send a ``webhook.ping`` through an outbound hook and report what the receiver said."""
    try:
        dispatcher = getattr(request.app.state, "webhooks", None)
        if dispatcher is None:
            raise HubError("webhooks_disabled", "outbound webhooks are disabled (CAT_FLEET_WEBHOOKS=0)", 409)
        hook = await _hub(request).webhook_with_secret(request.path_params["hook_id"])
        if hook["direction"] != "out":
            raise HubError("validation_error", "only outbound webhooks can be tested", 400)
        return JSONResponse(await dispatcher.send_test(hook))
    except HubError as exc:
        return _json_error(exc)


HOOK_BODY_MAX = 64 * 1024


async def _read_capped(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HubError("payload_too_large", f"body exceeds {limit} bytes", 413)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HubError("payload_too_large", f"body exceeds {limit} bytes", 413)
        chunks.append(chunk)
    return b"".join(chunks)


def _hook_authorized(hook: dict[str, Any], request: Request, raw: bytes) -> bool:
    """Bearer secret, or an HMAC-SHA256 signature of the raw body. Constant-time on bytes."""
    secret = hook["secret"]
    bearer = request.headers.get("authorization", "")
    if bearer and hmac.compare_digest(bearer.encode(), f"Bearer {secret}".encode()):
        return True
    signature = request.headers.get("x-catfleet-signature", "")
    return bool(signature) and hmac.compare_digest(signature.encode(), sign(secret, raw).encode())


async def inbound_hook(request: Request) -> JSONResponse:
    """``POST /hooks/in/{name}``: an external app posts a message into the hub.

    Outside ``Guard`` on purpose: the caller is not a fleet client and has no
    fleet token. The hook's own secret authenticates it instead. An unknown or
    disabled hook answers 404 so names cannot be probed.
    """
    try:
        hub = _hub(request)
        hook = await hub.webhook_secret_hook(request.path_params["name"], "in")
        if hook is None:
            raise HubError("not_found", "webhook not found", 404)
        raw = await _read_capped(request, HOOK_BODY_MAX)
        if not _hook_authorized(hook, request, raw):
            return _unauthorized()
        try:
            body = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise HubError("validation_error", "request body must be JSON", 400) from exc
        if not isinstance(body, dict):
            raise HubError("validation_error", "request body must be a JSON object", 400)
        return JSONResponse(await hub.post_via_webhook(hook, body), status_code=201)
    except HubError as exc:
        return _json_error(exc)


def _frame(event: dict[str, Any]) -> str:
    data = json.dumps(event, separators=(",", ":"))
    return f"id: {event['id']}\nevent: {event['kind']}\ndata: {data}\n\n"


async def events(request: Request) -> StreamingResponse:
    """SSE replay from the durable events table, then live wakes."""
    try:
        params = request.query_params
        after = optional_id(params.get("after_event_id", 0), "after_event_id") or 0
        channel_raw = params.get("channel")
        channel = channel_name(channel_raw) if channel_raw else None
        agent_raw = params.get("agent")
        agent = handle(agent_raw, "agent") if agent_raw else None
        kinds = event_kinds(params.get("kinds"))
    except HubError as exc:
        return _json_error(exc)  # type: ignore[return-value]

    hub = _hub(request)
    if channel is not None:
        known = {item["name"] for item in (await hub.list_channels())["channels"]}
        if channel not in known:
            return _json_error(HubError("not_found", f"channel {channel} not found", 404))

    async def generate():
        cursor = after
        try:
            if await hub.cursor_expired(cursor):
                yield 'event: reset\ndata: {"reason":"cursor_expired"}\n\n'
                return
            while True:
                if await request.is_disconnected():
                    return
                batch = await hub.events_after(
                    cursor, channel=channel, limit=SSE_BATCH, kinds=kinds
                )
                for event in batch:
                    # Filtered-out events still advance the cursor.
                    if agent_relevant(event, agent):
                        yield _frame(event)
                    cursor = int(event["id"])
                if len(batch) >= SSE_BATCH:
                    continue
                try:
                    async with hub.waiters.condition:
                        fresh = await hub.events_after(
                            cursor, channel=channel, limit=1, kinds=kinds
                        )
                        if fresh:
                            continue
                        await asyncio.wait_for(hub.waiters.condition.wait(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def api_routes() -> list[Route]:
    return [
        Route("/api/v1/channels", list_channels, methods=["GET"]),
        Route("/api/v1/channels", create_channel, methods=["POST"]),
        Route("/api/v1/channels/{name}/archive", archive_channel, methods=["POST"]),
        Route("/api/v1/channels/{name}/unarchive", unarchive_channel, methods=["POST"]),
        Route("/api/v1/channels/{name}/state", set_channel_state, methods=["POST"]),
        Route("/api/v1/attachments/{attachment_id:int}", get_attachment, methods=["GET"]),
        Route("/api/v1/notifications", notifications, methods=["GET"]),
        Route("/api/v1/events/cursor", event_cursor, methods=["GET"]),
        Route("/api/v1/messages", list_messages, methods=["GET"]),
        Route("/api/v1/messages", post_message, methods=["POST"]),
        Route("/api/v1/wait", wait_for_mentions, methods=["GET"]),
        Route("/api/v1/tasks", list_tasks, methods=["GET"]),
        Route("/api/v1/tasks", create_task, methods=["POST"]),
        Route("/api/v1/tasks/{task_id:int}/claim", claim_task, methods=["POST"]),
        Route("/api/v1/tasks/{task_id:int}/status", update_task_status, methods=["POST"]),
        Route("/api/v1/agents", list_agents, methods=["GET"]),
        Route("/api/v1/events", events, methods=["GET"]),
        Route("/api/v1/webhooks", list_webhooks, methods=["GET"]),
        Route("/api/v1/webhooks", create_webhook, methods=["POST"]),
        Route("/api/v1/webhooks/{hook_id:int}", get_webhook, methods=["GET"]),
        Route("/api/v1/webhooks/{hook_id:int}", update_webhook, methods=["PATCH"]),
        Route("/api/v1/webhooks/{hook_id:int}", delete_webhook, methods=["DELETE"]),
        Route("/api/v1/webhooks/{hook_id:int}/test", test_webhook, methods=["POST"]),
        Route("/api/v1/webhooks/{hook_id:int}/rotate-secret", rotate_webhook_secret, methods=["POST"]),
    ]


def hook_routes() -> list[Route]:
    """Inbound webhook receiver. Register before the SPA catch-all."""
    return [Route("/hooks/in/{name}", inbound_hook, methods=["POST"])]
