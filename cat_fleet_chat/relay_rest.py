"""REST routes for the relay control plane: durable consumers, relays and the audit log.

Same conventions as ``rest_app.py``: one handler per route, ``HubError`` to
the ``{"error": …}`` envelope, the acting handle from ``actor`` (or
``agent_name``) in the body, else the query string.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cat_fleet_chat.errors import HubError
from cat_fleet_chat.rest_app import _body, _caller, _hub, _json_error, _relay_caller


def _actor(request: Request, body: dict) -> str:
    """The acting handle. A relay token acts as its relay unless it names the human."""
    actor = _caller(request, body, "actor")
    if actor in (None, ""):
        actor = _relay_caller(request)
    if actor in (None, ""):
        raise HubError("validation_error", "actor is required", 400, {"field": "actor"})
    return actor


def _expected_version(request: Request, body: dict):
    """``If-Match: <version>``, else ``expected_version`` in the body or query."""
    return (
        request.headers.get("if-match")
        or body.get("expected_version")
        or request.query_params.get("expected_version")
    )


async def list_consumers(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).list_consumers(request.query_params.get("owner")))
    except HubError as exc:
        return _json_error(exc)


async def create_consumer(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).create_consumer(_actor(request, body), body)
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def get_consumer(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).get_consumer(request.path_params["name"]))
    except HubError as exc:
        return _json_error(exc)


async def update_consumer(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).update_consumer(
            _actor(request, body), request.path_params["name"], body
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def delete_consumer(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).delete_consumer(_actor(request, body), request.path_params["name"])
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def consumer_wait(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).consumer_wait(
            request.path_params["name"], params.get("timeout"), params.get("limit")
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def consumer_ack(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).consumer_ack(
            request.path_params["name"], body.get("cursor"), relay=_relay_caller(request)
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def consumer_seek(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).consumer_seek(
            _actor(request, body),
            request.path_params["name"],
            after_event_id=body.get("after_event_id"),
            at=body.get("at"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def list_audit(request: Request) -> JSONResponse:
    try:
        params = request.query_params
        result = await _hub(request).list_audit(
            params.get("target_kind"),
            params.get("target"),
            params.get("actor"),
            params.get("before_id"),
            params.get("limit"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


# ---- relays ------------------------------------------------------------------


async def list_relays(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).list_relays())
    except HubError as exc:
        return _json_error(exc)


async def create_relay(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        return JSONResponse(await _hub(request).create_relay(_actor(request, body), body), status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def get_relay(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).get_relay(request.path_params["relay"]))
    except HubError as exc:
        return _json_error(exc)


async def update_relay(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).update_relay(
            _actor(request, body),
            request.path_params["relay"],
            body,
            expected_version=_expected_version(request, body),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def delete_relay(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).delete_relay(
            _actor(request, body), request.path_params["relay"], expected_version=_expected_version(request, body)
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def rotate_relay_token(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        return JSONResponse(await _hub(request).rotate_relay_token(_actor(request, body), request.path_params["relay"]))
    except HubError as exc:
        return _json_error(exc)


async def relay_heartbeat(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        return JSONResponse(await _hub(request).relay_heartbeat(request.path_params["relay"], body.get("status")))
    except HubError as exc:
        return _json_error(exc)


async def relay_reply_target(request: Request) -> JSONResponse:
    try:
        return JSONResponse(
            await _hub(request).relay_reply_target(request.path_params["relay"], request.path_params["message_id"])
        )
    except HubError as exc:
        return _json_error(exc)


async def add_binding(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).add_binding(_actor(request, body), request.path_params["relay"], body)
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def update_binding(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).update_binding(
            _actor(request, body), request.path_params["relay"], request.path_params["binding_id"], body
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def delete_binding(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).delete_binding(
            _actor(request, body), request.path_params["relay"], request.path_params["binding_id"]
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def add_route(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).add_route(_actor(request, body), request.path_params["relay"], body)
        return JSONResponse(result, status_code=201)
    except HubError as exc:
        return _json_error(exc)


async def seek_route(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).seek_route(
            _actor(request, body),
            request.path_params["relay"],
            request.path_params["route_id"],
            after_event_id=body.get("after_event_id"),
            at=body.get("at"),
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def update_route(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).update_route(
            _actor(request, body), request.path_params["relay"], request.path_params["route_id"], body
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def delete_route(request: Request) -> JSONResponse:
    try:
        body = await _body(request)
        result = await _hub(request).delete_route(
            _actor(request, body), request.path_params["relay"], request.path_params["route_id"]
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


def relay_api_routes() -> list[Route]:
    return [
        Route("/api/v1/relays", list_relays, methods=["GET"]),
        Route("/api/v1/relays", create_relay, methods=["POST"]),
        Route("/api/v1/relays/{relay}", get_relay, methods=["GET"]),
        Route("/api/v1/relays/{relay}", update_relay, methods=["PATCH"]),
        Route("/api/v1/relays/{relay}", delete_relay, methods=["DELETE"]),
        Route("/api/v1/relays/{relay}/config", get_relay, methods=["GET"]),
        Route("/api/v1/relays/{relay}/rotate-token", rotate_relay_token, methods=["POST"]),
        Route("/api/v1/relays/{relay}/heartbeat", relay_heartbeat, methods=["POST"]),
        Route("/api/v1/relays/{relay}/reply-target/{message_id:int}", relay_reply_target, methods=["GET"]),
        Route("/api/v1/relays/{relay}/bindings", add_binding, methods=["POST"]),
        Route("/api/v1/relays/{relay}/bindings/{binding_id:int}", update_binding, methods=["PATCH"]),
        Route("/api/v1/relays/{relay}/bindings/{binding_id:int}", delete_binding, methods=["DELETE"]),
        Route("/api/v1/relays/{relay}/routes", add_route, methods=["POST"]),
        Route("/api/v1/relays/{relay}/routes/{route_id:int}", update_route, methods=["PATCH"]),
        Route("/api/v1/relays/{relay}/routes/{route_id:int}", delete_route, methods=["DELETE"]),
        Route("/api/v1/relays/{relay}/routes/{route_id:int}/seek", seek_route, methods=["POST"]),
        Route("/api/v1/consumers", list_consumers, methods=["GET"]),
        Route("/api/v1/consumers", create_consumer, methods=["POST"]),
        Route("/api/v1/consumers/{name}", get_consumer, methods=["GET"]),
        Route("/api/v1/consumers/{name}", update_consumer, methods=["PATCH"]),
        Route("/api/v1/consumers/{name}", delete_consumer, methods=["DELETE"]),
        Route("/api/v1/consumers/{name}/wait", consumer_wait, methods=["GET"]),
        Route("/api/v1/consumers/{name}/ack", consumer_ack, methods=["POST"]),
        Route("/api/v1/consumers/{name}/seek", consumer_seek, methods=["POST"]),
        Route("/api/v1/audit", list_audit, methods=["GET"]),
    ]
