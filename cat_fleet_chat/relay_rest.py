"""REST routes for the relay control plane: durable consumers and the audit log.

Same conventions as ``rest_app.py``: one handler per route, ``HubError`` to
the ``{"error": …}`` envelope, the acting handle from ``actor`` (or
``agent_name``) in the body, else the query string.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cat_fleet_chat.errors import HubError
from cat_fleet_chat.rest_app import _body, _caller, _hub, _json_error


def _actor(request: Request, body: dict) -> str:
    actor = _caller(request, body, "actor")
    if actor in (None, ""):
        raise HubError("validation_error", "actor is required", 400, {"field": "actor"})
    return actor


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
        return JSONResponse(await _hub(request).consumer_ack(request.path_params["name"], body.get("cursor")))
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


def relay_api_routes() -> list[Route]:
    return [
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
