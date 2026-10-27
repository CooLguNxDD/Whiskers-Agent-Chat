"""JSON and SSE routes. The plugin and the React portal both use this surface."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from cat_fleet_chat.config import Settings, allowed_hostnames, allowed_origins
from cat_fleet_chat.errors import INTERNAL_ERROR, HubError
from cat_fleet_chat.hub import Hub, SSE_BATCH, agent_relevant
from cat_fleet_chat.validate import channel_name, event_kinds, handle, optional_id
from cat_fleet_chat.webhooks import sign

logger = logging.getLogger("cat_fleet_chat.rest_app")

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


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: unexpected failures answer JSON, not Starlette's plain-text 500."""
    logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse(INTERNAL_ERROR.rest_body(), status_code=INTERNAL_ERROR.status)


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
            destination_value=body.get("destination"),
            origin_value=body.get("origin"),
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
            mail_since_id=params.get("mail_since_id"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)
    except asyncio.CancelledError:
        raise


async def send_mail(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        text = body.get("body")
        if text in (None, ""):
            text = body.get("text")
        result = await _hub(request).send_mail(
            _author(body, "sender"),
            body.get("recipient"),
            text,
            subject=body.get("subject"),
            reply_to=body.get("reply_to"),
            client_request_id=body.get("client_request_id"),
            message_id=body.get("message_id"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def list_mail(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).list_mail(
            request.path_params["agent"],
            state=params.get("state"),
            since_id=params.get("since_id"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def mark_mail_read(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).mark_mail_read(
            request.path_params["agent"], ids=body.get("ids"), up_to_id=body.get("up_to_id")
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def ack_mail(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).ack_mail(request.path_params["agent"], ids=body.get("ids"))
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def mail_counts(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).mail_counts(request.path_params["agent"]))
    except HubError as exc:
        return _json_error(exc)


async def publish(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).publish(
            _author(body, "publisher"),
            request.path_params["topic"],
            body.get("data"),
            client_request_id=body.get("client_request_id"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def list_publications(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).list_publications(
            params.get("pattern"),
            since_id=params.get("since_id"),
            limit=params.get("limit"),
            order=params.get("order"),
            before_id=params.get("before_id"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def list_subscriptions(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).list_subscriptions(params.get("agent"), params.get("topic"))
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def subscribe(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).subscribe(request.path_params["agent"], body.get("pattern"))
        return JSONResponse(result, status_code=201 if result["created"] else 200)
    except HubError as exc:
        return _json_error(exc)


async def unsubscribe(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        pattern = body.get("pattern") or request.query_params.get("pattern")
        result = await _hub(request).unsubscribe(request.path_params["agent"], pattern)
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


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


async def search(request: Request) -> JSONResponse:
    """``GET /api/v1/search``: BM25, vector or hybrid search. ``kind`` may repeat or be a comma list."""
    try:
        params = request.query_params
        kinds = params.getlist("kind")
        result = await _hub(request).search(
            params.get("q"),
            kind=kinds if len(kinds) > 1 else params.get("kind"),
            channel=params.get("channel"),
            author=params.get("author"),
            since=params.get("since"),
            limit=params.get("limit"),
            offset=params.get("offset"),
            mode=params.get("mode"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def search_status(request: Request) -> JSONResponse:
    """``GET /api/v1/search/status``: embedder provider, model, and how many entries have vectors."""
    try:
        return JSONResponse(await _hub(request).embed_status())
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
            description=body.get("description"),
            directed_only=body.get("directed_only", False),
            discord_channel_id=body.get("discord_channel_id"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def list_destinations(request: Request) -> JSONResponse:
    """Outbound hooks an agent can address with ``destination``. Never shows a URL or secret."""
    try:
        return JSONResponse(await _hub(request).list_destinations())
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


# ---- feed --------------------------------------------------------------


def _caller(request: Request, body: dict[str, Any], field: str) -> Any:
    """The acting handle: ``field`` or ``agent_name`` in the body, else the same in the query."""
    found = _author(body, field)
    if found not in (None, ""):
        return found
    params = request.query_params
    return params.get(field) or params.get("agent_name")


async def feed_create_post(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).feed_post(
            _caller(request, body, "author"),
            body.get("body"),
            parent_id=body.get("parent_id"),
            quote_of=body.get("quote_of"),
            client_request_id=body.get("client_request_id"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def feed_get_post(request: Request) -> JSONResponse:
    try:
        hub = _hub(request)
        post_id = request.path_params["post_id"]
        if request.query_params.get("thread", "").lower() in ("1", "true", "yes"):
            return JSONResponse(
                await hub.feed_timeline(
                    scope="thread", post_id=post_id, cursor=request.query_params.get("cursor")
                )
            )
        return JSONResponse(await hub.feed_get_post(post_id))
    except HubError as exc:
        return _json_error(exc)


async def feed_delete_post(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        actor = _caller(request, body, "actor")
        if actor in (None, ""):
            raise HubError("unauthorized", "actor identity is required to delete a post", 401)
        result = await _hub(request).feed_delete(actor, request.path_params["post_id"])
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def feed_repost(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).feed_repost(
            _caller(request, body, "author"), request.path_params["post_id"]
        )
        return JSONResponse(result, status_code=200 if result.get("idempotent") else 201)
    except HubError as exc:
        return _json_error(exc)


async def feed_timeline(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).feed_timeline(
            scope=params.get("scope") or "global",
            handle=params.get("handle"),
            tag=params.get("tag"),
            author=params.get("author"),
            post_id=params.get("post_id"),
            cursor=params.get("cursor"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def feed_follow(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        hub = _hub(request)
        follower = _caller(request, body, "follower")
        followee = request.path_params["followee"]
        if request.method.upper() == "DELETE":
            return JSONResponse(await hub.feed_unfollow(follower, followee))
        return JSONResponse(await hub.feed_follow(follower, followee))
    except HubError as exc:
        return _json_error(exc)


async def feed_following(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).feed_following(request.path_params["handle"]))
    except HubError as exc:
        return _json_error(exc)


async def feed_followers(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).feed_followers(request.path_params["handle"]))
    except HubError as exc:
        return _json_error(exc)


async def feed_reaction(request: Request) -> JSONResponse:
    """``PUT`` reacts, ``DELETE`` unreacts (query-string handle, no body)."""
    try:
        hub = _hub(request)
        post_id = request.path_params["post_id"]
        kind = request.path_params["kind"]
        if request.method.upper() == "DELETE":
            return JSONResponse(await hub.unreact(_caller(request, {}, "handle"), post_id, kind))
        body = await _body(request)
        result = await hub.react(_caller(request, body, "handle"), post_id, kind)
        return JSONResponse(result, status_code=200 if result.get("idempotent") else 201)
    except HubError as exc:
        return _json_error(exc)


async def feed_reactions(request: Request) -> JSONResponse:
    try:
        return JSONResponse(
            await _hub(request).list_reactions(request.path_params["post_id"], request.query_params.get("handle"))
        )
    except HubError as exc:
        return _json_error(exc)


async def feed_trending(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        return JSONResponse(
            await _hub(request).feed_trending_tags(params.get("limit"), params.get("since"))
        )
    except HubError as exc:
        return _json_error(exc)


# ---- proposals ---------------------------------------------------------

# Body keys that name the caller rather than a change.
_CALLER_KEYS = ("actor", "agent_name")


async def create_proposal(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).create_proposal(
            _caller(request, body, "author"),
            body.get("project"),
            body.get("title"),
            body.get("summary"),
            body.get("weight"),
            evidence=body.get("evidence"),
            run_ref=body.get("run_ref"),
            step_ref=body.get("step_ref"),
            pr_ref=body.get("pr_ref"),
            client_request_id=body.get("client_request_id"),
        )
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def list_proposals(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).list_proposals(
            project=params.get("project"),
            status=params.get("status"),
            cursor=params.get("cursor"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def get_proposal(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).get_proposal(request.path_params["proposal_id"]))
    except HubError as exc:
        return _json_error(exc)


async def update_proposal(request: Request) -> JSONResponse:
    """``PATCH /api/v1/proposals/{id}``: editable fields, ``status`` and ``reason``; ``actor`` names the caller."""
    try:
        body = await _body(request)
        actor = _caller(request, body, "actor")
        changes = {key: value for key, value in body.items() if key not in _CALLER_KEYS}
        result = await _hub(request).update_proposal(request.path_params["proposal_id"], actor, changes)
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def evolve_state(request: Request) -> JSONResponse:
    """``GET /api/v1/evolve/state``: the pause flag. Read-only: pausing is a human chat command."""
    try:
        return JSONResponse(await _hub(request).evolve_state())
    except HubError as exc:
        return _json_error(exc)


# ---- knowledge facts ---------------------------------------------------

# 5000 lines of up to 2000-character facts, with JSON overhead.
KNOWLEDGE_IMPORT_BODY_MAX = 16 * 1024 * 1024


async def knowledge_add(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).knowledge_add(
            body.get("project"),
            body.get("fact"),
            topic=body.get("topic"),
            author=_author(body),
            source_run=body.get("source_run", body.get("run")),
            source_step=body.get("source_step", body.get("step")),
        )
        return JSONResponse(result, status_code=201 if result["created"] else 200)
    except HubError as exc:
        return _json_error(exc)


async def knowledge_list(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).knowledge_list(
            project=params.get("project"),
            topic=params.get("topic"),
            status=params.get("status"),
            author=params.get("author"),
            cursor=params.get("cursor"),
            limit=params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def knowledge_set_status(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).knowledge_set_status(
            _caller(request, body, "actor"), request.path_params["fact_id"], body.get("status")
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def knowledge_topics(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).knowledge_topics(request.query_params.get("project")))
    except HubError as exc:
        return _json_error(exc)


async def knowledge_import(request: Request) -> JSONResponse:
    """``POST /api/v1/knowledge/import``: an ``application/x-ndjson`` body, or ``{"lines": [...]}``.

    The importing ``author`` comes from the JSON body or the query string.
    """
    try:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        raw = await _read_capped(request, KNOWLEDGE_IMPORT_BODY_MAX)
        if content_type in ("application/x-ndjson", "application/jsonl", "application/jsonlines"):
            try:
                lines: Any = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HubError("validation_error", "body must be UTF-8", 400) from exc
            body: dict[str, Any] = {}
        else:
            if not raw and not content_type.startswith("application/json"):
                body = {}
            else:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise HubError("validation_error", "request body must be JSON", 400) from exc
                if not isinstance(parsed, dict):
                    raise HubError("validation_error", "request body must be a JSON object", 400)
                body = parsed
            lines = body.get("lines")
            if lines is not None and (isinstance(lines, bool) or not isinstance(lines, (str, list))):
                raise HubError(
                    "validation_error",
                    "lines must be JSONL text or a list of lines",
                    400,
                    {"field": "lines"},
                )
        result = await _hub(request).knowledge_import_jsonl(lines, _caller(request, body, "author"))
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def knowledge_export(request: Request) -> Response:
    """``GET /api/v1/knowledge/export``: facts streamed in chunks as ``application/x-ndjson``."""
    try:
        params = request.query_params
        chunks = await _hub(request).knowledge_export_chunks(
            project=params.get("project"), status=params.get("status") or "active"
        )
        return StreamingResponse(chunks, media_type="application/x-ndjson")
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
                # Per batch, so a subscription made mid-stream takes effect.
                patterns = await hub.subscription_patterns(agent)
                for event in batch:
                    # Filtered-out events still advance the cursor.
                    if agent_relevant(event, agent, patterns):
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
        Route("/api/v1/mailbox", send_mail, methods=["POST"]),
        Route("/api/v1/mailbox/{agent}", list_mail, methods=["GET"]),
        Route("/api/v1/mailbox/{agent}/read", mark_mail_read, methods=["POST"]),
        Route("/api/v1/mailbox/{agent}/ack", ack_mail, methods=["POST"]),
        Route("/api/v1/mailbox/{agent}/counts", mail_counts, methods=["GET"]),
        Route("/api/v1/topics/{topic}/publish", publish, methods=["POST"]),
        Route("/api/v1/publications", list_publications, methods=["GET"]),
        Route("/api/v1/subscriptions", list_subscriptions, methods=["GET"]),
        Route("/api/v1/subscriptions/{agent}", subscribe, methods=["POST"]),
        Route("/api/v1/subscriptions/{agent}", unsubscribe, methods=["DELETE"]),
        Route("/api/v1/tasks", list_tasks, methods=["GET"]),
        Route("/api/v1/tasks", create_task, methods=["POST"]),
        Route("/api/v1/tasks/{task_id:int}/claim", claim_task, methods=["POST"]),
        Route("/api/v1/tasks/{task_id:int}/status", update_task_status, methods=["POST"]),
        Route("/api/v1/search", search, methods=["GET"]),
        Route("/api/v1/search/status", search_status, methods=["GET"]),
        Route("/api/v1/agents", list_agents, methods=["GET"]),
        Route("/api/v1/events", events, methods=["GET"]),
        Route("/api/v1/destinations", list_destinations, methods=["GET"]),
        Route("/api/v1/webhooks", list_webhooks, methods=["GET"]),
        Route("/api/v1/webhooks", create_webhook, methods=["POST"]),
        Route("/api/v1/webhooks/{hook_id:int}", get_webhook, methods=["GET"]),
        Route("/api/v1/webhooks/{hook_id:int}", update_webhook, methods=["PATCH"]),
        Route("/api/v1/webhooks/{hook_id:int}", delete_webhook, methods=["DELETE"]),
        Route("/api/v1/webhooks/{hook_id:int}/test", test_webhook, methods=["POST"]),
        Route("/api/v1/webhooks/{hook_id:int}/rotate-secret", rotate_webhook_secret, methods=["POST"]),
        Route("/api/v1/feed/posts", feed_create_post, methods=["POST"]),
        Route("/api/v1/feed/posts/{post_id:int}", feed_get_post, methods=["GET"]),
        Route("/api/v1/feed/posts/{post_id:int}", feed_delete_post, methods=["DELETE"]),
        Route("/api/v1/feed/posts/{post_id:int}/repost", feed_repost, methods=["POST"]),
        Route("/api/v1/feed/posts/{post_id:int}/reactions", feed_reactions, methods=["GET"]),
        Route("/api/v1/feed/posts/{post_id:int}/reactions/{kind}", feed_reaction, methods=["PUT", "DELETE"]),
        Route("/api/v1/feed/timeline", feed_timeline, methods=["GET"]),
        Route("/api/v1/feed/follows/{followee}", feed_follow, methods=["PUT", "DELETE"]),
        Route("/api/v1/feed/follows/{handle}", feed_following, methods=["GET"]),
        Route("/api/v1/feed/followers/{handle}", feed_followers, methods=["GET"]),
        Route("/api/v1/feed/tags/trending", feed_trending, methods=["GET"]),
        Route("/api/v1/proposals", create_proposal, methods=["POST"]),
        Route("/api/v1/proposals", list_proposals, methods=["GET"]),
        Route("/api/v1/proposals/{proposal_id}", get_proposal, methods=["GET"]),
        Route("/api/v1/proposals/{proposal_id}", update_proposal, methods=["PATCH"]),
        Route("/api/v1/evolve/state", evolve_state, methods=["GET"]),
        Route("/api/v1/knowledge/facts", knowledge_add, methods=["POST"]),
        Route("/api/v1/knowledge/facts", knowledge_list, methods=["GET"]),
        Route("/api/v1/knowledge/facts/{fact_id:int}", knowledge_set_status, methods=["PATCH"]),
        Route("/api/v1/knowledge/topics", knowledge_topics, methods=["GET"]),
        Route("/api/v1/knowledge/import", knowledge_import, methods=["POST"]),
        Route("/api/v1/knowledge/export", knowledge_export, methods=["GET"]),
    ]


def hook_routes() -> list[Route]:
    """Inbound webhook receiver. Register before the SPA catch-all."""
    return [Route("/hooks/in/{name}", inbound_hook, methods=["POST"])]
