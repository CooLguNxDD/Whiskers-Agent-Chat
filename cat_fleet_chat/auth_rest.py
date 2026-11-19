"""REST routes for basic login, account management, and SQLite-backed sessions."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cat_fleet_chat import auth
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.rest_app import _body, _hub, _json_error


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",", 1)[0].strip().lower()
    return first == "https"


def _set_session_cookie(
    response: JSONResponse, request: Request, raw_token: str, *, max_age: int | None
) -> None:
    response.set_cookie(
        key=auth.SESSION_COOKIE,
        value=raw_token,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


def _clear_session_cookie(response: JSONResponse, request: Request) -> None:
    response.delete_cookie(
        key=auth.SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _bool_field(body: dict[str, Any], field: str, default: bool) -> bool:
    if field not in body or body[field] is None:
        return default
    value = body[field]
    if not isinstance(value, bool):
        raise HubError(
            "validation_error", f"{field} must be a boolean", 400, {"field": field}
        )
    return value


async def auth_status(request: Request) -> JSONResponse:
    try:
        settings = request.app.state.settings
        state = request.scope.get("state", {})
        auth_kind = state.get("auth_kind")
        has_users = await _hub(request).auth_has_users()
        return JSONResponse(
            {
                "auth_required": bool(settings.token),
                "authenticated": bool(auth_kind),
                "auth_method": auth_kind,
                "has_users": has_users,
                "user": state.get("user"),
                "session": state.get("session"),
            }
        )
    except HubError as exc:
        return _json_error(exc)


async def _login_with_token(
    request: Request,
    body: dict[str, Any],
    *,
    ip: str,
    ua: str,
    max_age: int | None,
) -> JSONResponse:
    settings = request.app.state.settings
    limiter = request.app.state.login_limiter
    if settings.token is None:
        raise HubError(
            "validation_error",
            "hub token login is only available when CAT_FLEET_TOKEN is set",
            400,
        )
    presented = body.get("token")
    if not isinstance(presented, str) or not hmac.compare_digest(presented, settings.token):
        limiter.record_failure(ip)
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": "invalid hub token"}},
            status_code=401,
        )
    limiter.reset(ip)
    result = await _hub(request).login_with_token(
        label_value=body.get("label"),
        user_agent=ua,
        ip_address=ip,
        ttl_days=settings.session_ttl_days,
    )
    response = JSONResponse(
        {
            "authenticated": True,
            "auth_method": "session",
            "user": None,
            "session": result["session"],
        }
    )
    _set_session_cookie(response, request, result["raw_token"], max_age=max_age)
    return response


async def _login_with_password(
    request: Request,
    body: dict[str, Any],
    *,
    ip: str,
    ua: str,
    max_age: int | None,
) -> JSONResponse:
    settings = request.app.state.settings
    limiter = request.app.state.login_limiter
    result = await _hub(request).login_with_password(
        body.get("username"),
        body.get("password"),
        label_value=body.get("label"),
        user_agent=ua,
        ip_address=ip,
        ttl_days=settings.session_ttl_days,
    )
    if result is None:
        limiter.record_failure(ip)
        return JSONResponse(
            {"error": {"code": "unauthorized", "message": "invalid username or password"}},
            status_code=401,
        )
    limiter.reset(ip)
    response = JSONResponse(
        {
            "authenticated": True,
            "auth_method": "session",
            "user": result["user"],
            "session": result["session"],
        }
    )
    _set_session_cookie(response, request, result["raw_token"], max_age=max_age)
    return response


_LOGIN_MODES: tuple[tuple[str, Callable[[dict[str, Any]], bool]], ...] = (
    ("token", lambda body: body.get("token") not in (None, "")),
    ("password", lambda body: "username" in body or "password" in body),
)

_LOGIN_HANDLERS: dict[str, Callable[..., Awaitable[JSONResponse]]] = {
    "token": _login_with_token,
    "password": _login_with_password,
}


async def login(request: Request) -> JSONResponse:
    try:
        settings = request.app.state.settings
        limiter = request.app.state.login_limiter
        ip = _client_ip(request)
        limiter.check(ip)

        body = await _body(request)
        remember = _bool_field(body, "remember", True)
        max_age = settings.session_ttl_days * 86400 if remember else None
        ua = request.headers.get("user-agent", "")

        mode = next((name for name, matcher in _LOGIN_MODES if matcher(body)), None)
        if mode is None:
            raise HubError(
                "validation_error",
                "provide username and password, or token",
                400,
            )
        return await _LOGIN_HANDLERS[mode](request, body, ip=ip, ua=ua, max_age=max_age)
    except HubError as exc:
        return _json_error(exc)


async def register(request: Request) -> JSONResponse:
    try:
        settings = request.app.state.settings
        hub = _hub(request)
        state = request.scope.get("state", {})
        limiter = request.app.state.login_limiter
        ip = _client_ip(request)
        authed = state.get("auth_kind") in ("bearer", "session")

        body = await _body(request)
        if settings.token is not None and not authed:
            limiter.check(ip)
            presented = body.get("token")
            if not isinstance(presented, str) or not hmac.compare_digest(
                presented, settings.token
            ):
                limiter.record_failure(ip)
                return JSONResponse(
                    {
                        "error": {
                            "code": "unauthorized",
                            "message": "valid hub token required to create an account",
                        }
                    },
                    status_code=401,
                )
            limiter.reset(ip)

        sign_in = _bool_field(body, "sign_in", not authed)
        remember = _bool_field(body, "remember", True)
        max_age = settings.session_ttl_days * 86400 if remember else None

        result = await hub.register_user(
            body.get("username"),
            body.get("password"),
            display_name_value=body.get("display_name"),
            create_session=sign_in,
            label_value=body.get("label"),
            user_agent=request.headers.get("user-agent", ""),
            ip_address=ip,
            ttl_days=settings.session_ttl_days,
        )
        response = JSONResponse(
            {"user": result["user"], "session": result["session"]},
            status_code=201,
        )
        if sign_in and result["raw_token"]:
            _set_session_cookie(response, request, result["raw_token"], max_age=max_age)
        return response
    except HubError as exc:
        return _json_error(exc)


async def logout(request: Request) -> JSONResponse:
    try:
        sess = request.scope.get("state", {}).get("session")
        if sess is not None:
            await _hub(request).logout_session(int(sess["id"]))
        response = JSONResponse({"ok": True})
        _clear_session_cookie(response, request)
        return response
    except HubError as exc:
        return _json_error(exc)


async def list_sessions(request: Request) -> JSONResponse:
    try:
        sess = request.scope.get("state", {}).get("session")
        current_id = int(sess["id"]) if sess is not None else None
        return JSONResponse(await _hub(request).list_sessions(current_session_id=current_id))
    except HubError as exc:
        return _json_error(exc)


async def revoke_session(request: Request) -> JSONResponse:
    try:
        session_id = request.path_params["session_id"]
        result = await _hub(request).revoke_session(session_id)
        response = JSONResponse(result)
        sess = request.scope.get("state", {}).get("session")
        if sess is not None and int(sess["id"]) == int(session_id):
            _clear_session_cookie(response, request)
        return response
    except HubError as exc:
        return _json_error(exc)


async def revoke_other_sessions(request: Request) -> JSONResponse:
    try:
        sess = request.scope.get("state", {}).get("session")
        current_id = int(sess["id"]) if sess is not None else None
        return JSONResponse(await _hub(request).revoke_other_sessions(current_id))
    except HubError as exc:
        return _json_error(exc)


async def list_users(request: Request) -> JSONResponse:
    try:
        return JSONResponse(await _hub(request).list_users())
    except HubError as exc:
        return _json_error(exc)


async def change_password(request: Request) -> JSONResponse:
    try:
        state = request.scope.get("state", {})
        user = state.get("user")
        if user is None:
            raise HubError(
                "validation_error",
                "sign in with a user account to change password",
                400,
            )
        body = await _body(request)
        revoke_others = _bool_field(body, "revoke_others", False)
        sess = state.get("session")
        current_id = int(sess["id"]) if sess is not None else None
        result = await _hub(request).change_password(
            int(user["id"]),
            body.get("current_password"),
            body.get("new_password"),
            keep_session_id=current_id,
            revoke_others=revoke_others,
        )
        return JSONResponse(result)
    except HubError as exc:
        return _json_error(exc)


async def delete_user(request: Request) -> JSONResponse:
    try:
        user_id = request.path_params["user_id"]
        result = await _hub(request).delete_user(user_id)
        response = JSONResponse(result)
        user = request.scope.get("state", {}).get("user")
        if user is not None and int(user["id"]) == int(user_id):
            _clear_session_cookie(response, request)
        return response
    except HubError as exc:
        return _json_error(exc)


_ROUTE_HANDLERS: dict[str, Callable[[Request], Awaitable[JSONResponse]]] = {
    "status": auth_status,
    "login": login,
    "register": register,
    "logout": logout,
    "list_sessions": list_sessions,
    "revoke_other_sessions": revoke_other_sessions,
    "revoke_session": revoke_session,
    "list_users": list_users,
    "delete_user": delete_user,
    "change_password": change_password,
}


def auth_api_routes() -> list[Route]:
    return [
        Route(spec["path"], _ROUTE_HANDLERS[spec["id"]], methods=[spec["method"]])
        for spec in auth.AUTH_ROUTE_SPECS
    ]
