"""Auth, user account, SQLite session, and contract tests (schema v16)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from cat_fleet_chat import auth
from cat_fleet_chat.auth_rest import auth_api_routes
from cat_fleet_chat.config import Settings, load_settings
from cat_fleet_chat.db import SCHEMA_VERSION, Database
from cat_fleet_chat.rest_app import PUBLIC_AUTH_ROUTES
from conftest import _open


def _extract_cookie(response) -> tuple[str, dict[str, str]]:
    headers = response.headers.get_list("set-cookie")
    assert headers, "expected Set-Cookie header"
    cookie = SimpleCookie()
    for header in headers:
        cookie.load(header)
    morsel = cookie[auth.SESSION_COOKIE]
    attrs = {k.lower(): str(v) for k, v in morsel.items() if v != "" or k.lower() in {"httponly", "secure"}}
    # Also parse raw flags since SimpleCookie stores boolean flags with empty strings.
    raw = headers[-1]
    parts = [segment.strip() for segment in raw.split(";")[1:]]
    flags: dict[str, str] = {}
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            flags[k.strip().lower()] = v.strip()
        else:
            flags[part.lower()] = ""
    return morsel.value, flags


def _cookie_headers(raw_token: str) -> dict[str, str]:
    return {"Cookie": f"{auth.SESSION_COOKIE}={raw_token}"}


def _pw(tag: str) -> str:
    return f"fixture-{tag}-01234567"


async def test_schema_v16_creates_users_and_sessions_idempotently(tmp_path):
    db_path = str(tmp_path / "v16.sqlite")
    db = Database(db_path)
    await db.open()
    await db.close()
    # Second open proves migration is idempotent.
    db2 = Database(db_path)
    await db2.open()
    await db2.close()

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        versions = [
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
    assert SCHEMA_VERSION >= 16
    assert {"users", "sessions"} <= tables
    assert {"idx_sessions_user_id", "idx_sessions_expires_at"} <= indexes
    assert versions == list(range(1, SCHEMA_VERSION + 1))


def test_session_ttl_days_config_validation(monkeypatch):
    monkeypatch.delenv("CAT_FLEET_SESSION_TTL_DAYS", raising=False)
    assert load_settings().session_ttl_days == auth.SESSION_TTL_DEFAULT

    monkeypatch.setenv("CAT_FLEET_SESSION_TTL_DAYS", "1")
    assert load_settings().session_ttl_days == 1
    monkeypatch.setenv("CAT_FLEET_SESSION_TTL_DAYS", "365")
    assert load_settings().session_ttl_days == 365

    for bad in ("0", "366", "abc"):
        monkeypatch.setenv("CAT_FLEET_SESSION_TTL_DAYS", bad)
        with pytest.raises(SystemExit):
            load_settings()


def test_auth_contract_and_user_agent_table():
    routes = auth_api_routes()
    wired = {(next(iter(route.methods - {"HEAD"})), route.path) for route in routes}
    expected = {(spec["method"], spec["path"]) for spec in auth.AUTH_ROUTE_SPECS}
    assert wired == expected
    assert PUBLIC_AUTH_ROUTES == frozenset(
        (spec["method"], spec["path"]) for spec in auth.AUTH_ROUTE_SPECS if spec.get("public")
    )

    assert auth.summarize_user_agent("") == "Browser session"
    assert auth.summarize_user_agent("curl/8.4.0") == "API client"
    assert (
        auth.summarize_user_agent(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Edg/120.0"
        )
        == "Edge on Windows"
    )
    assert (
        auth.summarize_user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
        )
        == "Chrome on macOS"
    )
    assert (
        auth.summarize_user_agent(
            "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"
        )
        == "Firefox on Linux"
    )
    assert (
        auth.summarize_user_agent(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"
        )
        == "Safari on iOS"
    )
    assert (
        auth.summarize_user_agent("Mozilla/5.0 (Linux; Android 14) Chrome/120.0 Mobile")
        == "Chrome on Android"
    )


async def test_hub_token_login_cookie_authenticates_rest_and_sse_but_not_mcp(authed):
    client, app = authed
    status_before = await client.get("/api/v1/auth/status")
    assert status_before.status_code == 200
    assert status_before.json() == {
        "auth_required": True,
        "authenticated": False,
        "auth_method": None,
        "has_users": False,
        "user": None,
        "session": None,
    }

    bad_login = await client.post("/api/v1/auth/login", json={"token": "wrong-token"})
    assert bad_login.status_code == 401

    login_res = await client.post(
        "/api/v1/auth/login",
        json={"token": "test-token", "label": "Work tab"},
    )
    assert login_res.status_code == 200
    body = login_res.json()
    assert body["authenticated"] is True
    assert body["auth_method"] == "session"
    assert body["user"] is None
    assert body["session"]["auth_method"] == "token"
    assert body["session"]["label"] == "Work tab"
    assert body["session"]["current"] is True

    raw_cookie, flags = _extract_cookie(login_res)
    assert raw_cookie.startswith(auth.SESSION_PREFIX)
    assert "httponly" in flags
    assert flags.get("samesite", "").lower() == "lax"
    assert flags.get("path") == "/"
    assert flags.get("max-age") == str(30 * 86400)
    assert "secure" not in flags

    # Separate client without cookie jar: authenticates via Cookie header alone.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as clean:
        channels = await clean.get("/api/v1/channels", headers=_cookie_headers(raw_cookie))
        assert channels.status_code == 200

        posted = await clean.post(
            "/api/v1/messages",
            headers=_cookie_headers(raw_cookie),
            json={"channel": "fleet", "author": "ada", "text": "cookie post"},
        )
        assert posted.status_code == 201

        # Invalid Authorization header never falls back to a valid cookie.
        wrong_bearer = await clean.get(
            "/api/v1/channels",
            headers={**_cookie_headers(raw_cookie), "Authorization": "Bearer wrong"},
        )
        assert wrong_bearer.status_code == 401

        # Session cookie must NOT authenticate /mcp.
        mcp_res = await clean.post(
            "/mcp",
            headers=_cookie_headers(raw_cookie),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        assert mcp_res.status_code == 401

    # SSE stream works with the session cookie alone.
    bodies: list[bytes] = []
    ready = asyncio.Event()
    saw_event = asyncio.Event()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await saw_event.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
            ready.set()
        elif message["type"] == "http.response.body":
            bodies.append(message.get("body", b""))
            if b"message.created" in b"".join(bodies):
                saw_event.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"after_event_id=0",
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", f"{auth.SESSION_COOKIE}={raw_cookie}".encode("ascii")),
        ],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 123),
    }
    stream = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(ready.wait(), timeout=2)
    await asyncio.wait_for(saw_event.wait(), timeout=2)
    await asyncio.wait_for(stream, timeout=2)
    assert "cookie post" in b"".join(bodies).decode("utf-8")


async def test_cookie_flags_remember_false_and_https_secure(authed):
    client, _app = authed
    res = await client.post(
        "/api/v1/auth/login",
        headers={"X-Forwarded-Proto": "https"},
        json={"token": "test-token", "remember": False},
    )
    assert res.status_code == 200
    _raw, flags = _extract_cookie(res)
    assert "max-age" not in flags
    assert "secure" in flags
    assert "httponly" in flags


async def test_session_cookie_never_grants_reserved_author_provenance(tmp_path):
    settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "hub.sqlite"),
        token="test-token",
        dev_origins=("http://127.0.0.1:3001",),
        webhooks_enabled=False,
        reserved_author_tokens=frozenset({"fleet-secret"}),
    )
    async for client, _app in _open(settings):
        # Reserved-author secrets are not accepted by browser token login.
        bad = await client.post("/api/v1/auth/login", json={"token": "fleet-secret"})
        assert bad.status_code == 401

        login = await client.post("/api/v1/auth/login", json={"token": "test-token"})
        assert login.status_code == 200
        raw, _ = _extract_cookie(login)
        posted = await client.post(
            "/api/v1/messages",
            headers=_cookie_headers(raw),
            json={"channel": "fleet", "author": "cat-fleet", "text": "spoof"},
        )
        assert posted.status_code == 403


async def test_register_login_and_rate_limiting(authed):
    client, app = authed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as clean:
        # Unauthenticated registration requires the hub token.
        denied = await clean.post(
            "/api/v1/auth/register",
            json={"username": "ada", "password": _pw("1")},
        )
        assert denied.status_code == 401

        bad_user = await clean.post(
            "/api/v1/auth/register",
            json={"username": "x", "password": _pw("1"), "token": "test-token"},
        )
        assert bad_user.status_code == 400

        bad_pw = await clean.post(
            "/api/v1/auth/register",
            json={"username": "ada", "password": "x" * 5, "token": "test-token"},
        )
        assert bad_pw.status_code == 400

        created = await clean.post(
            "/api/v1/auth/register",
            json={"username": "Ada", "password": _pw("1"), "token": "test-token"},
        )
        assert created.status_code == 201
        created_body = created.json()
        assert created_body["user"]["username"] == "ada"
        assert created_body["user"]["display_name"] == "Ada"
        assert "password_hash" not in created_body["user"]
        assert created_body["session"] is not None
        raw1, _ = _extract_cookie(created)

        # Authenticated caller can create a second user without token (sign_in defaults False).
        second = await clean.post(
            "/api/v1/auth/register",
            headers=_cookie_headers(raw1),
            json={"username": "bob", "password": _pw("2")},
        )
        assert second.status_code == 201
        assert second.json()["session"] is None
        assert "set-cookie" not in second.headers

        dup = await clean.post(
            "/api/v1/auth/register",
            headers=_cookie_headers(raw1),
            json={"username": "ADA", "password": _pw("3")},
        )
        assert dup.status_code == 409

        # Case-insensitive password login.
        wrong_pw = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ADA", "password": _pw("wrong-pw")},
        )
        assert wrong_pw.status_code == 401
        unknown_user = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ghost", "password": _pw("1")},
        )
        assert unknown_user.status_code == 401

        # 2 failures so far; 7 more = 9 total, then success resets the window.
        for _ in range(7):
            fail = await clean.post(
                "/api/v1/auth/login",
                json={"username": "ada", "password": _pw("wrong")},
            )
            assert fail.status_code == 401

        ok_login = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ADA", "password": _pw("1")},
        )
        assert ok_login.status_code == 200
        assert ok_login.json()["user"]["last_login_at"] is not None

        # After reset, 10 failures trigger 429 on the 11th attempt.
        for _ in range(10):
            fail = await clean.post(
                "/api/v1/auth/login",
                json={"username": "ada", "password": _pw("wrong")},
            )
            assert fail.status_code == 401
        blocked = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ada", "password": _pw("1")},
        )
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"


async def test_register_works_without_token_in_open_mode(running):
    client, _app = running
    res = await client.post(
        "/api/v1/auth/register",
        json={"username": "localcat", "password": _pw("1")},
    )
    assert res.status_code == 201
    assert res.json()["user"]["username"] == "localcat"


async def test_session_management_revocation_touch_and_expiry(authed):
    client, app = authed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as clean:
        await clean.post(
            "/api/v1/auth/register",
            json={"username": "ada", "password": _pw("1"), "token": "test-token", "label": "S1"},
        )
        s1_login = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ada", "password": _pw("1"), "label": "Session 1"},
        )
        s2_login = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ada", "password": _pw("1"), "label": "Session 2"},
        )
        s3_login = await clean.post(
            "/api/v1/auth/login",
            json={"token": "test-token", "label": "Session 3"},
        )
        raw1, _ = _extract_cookie(s1_login)
        raw2, _ = _extract_cookie(s2_login)
        raw3, _ = _extract_cookie(s3_login)
        id1 = s1_login.json()["session"]["id"]
        id2 = s2_login.json()["session"]["id"]

        listed = await clean.get("/api/v1/auth/sessions", headers=_cookie_headers(raw1))
        assert listed.status_code == 200
        sessions = listed.json()["sessions"]
        assert len(sessions) == 4
        assert all("token_hash" not in item for item in sessions)
        assert [item["id"] for item in sessions if item["current"]] == [id1]

        # Revoke session 2 from session 1 -> session 2 immediately gets 401.
        revoked = await clean.delete(f"/api/v1/auth/sessions/{id2}", headers=_cookie_headers(raw1))
        assert revoked.status_code == 200
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw2))).status_code == 401

        # Revoke other sessions from session 1 -> only session 1 remains.
        rev_others = await clean.post(
            "/api/v1/auth/sessions/revoke-others",
            headers=_cookie_headers(raw1),
            json={},
        )
        assert rev_others.status_code == 200
        assert rev_others.json()["revoked_count"] == 2
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw3))).status_code == 401

        # Session touch updates last_seen_at after SESSION_TOUCH_INTERVAL.
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        with sqlite3.connect(app.state.settings.db_path) as conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (old_ts, id1))
            conn.commit()
        touched = await clean.get("/api/v1/auth/status", headers=_cookie_headers(raw1))
        assert touched.status_code == 200
        assert touched.json()["session"]["last_seen_at"] > old_ts

        # Logout deletes session from SQLite and clears cookie.
        logged_out = await clean.post("/api/v1/auth/logout", headers=_cookie_headers(raw1), json={})
        assert logged_out.status_code == 200
        cleared_val, cleared_flags = _extract_cookie(logged_out)
        assert cleared_val == ""
        assert cleared_flags.get("max-age") == "0"
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw1))).status_code == 401

        # Expired session is rejected and pruned from SQLite.
        s4_login = await clean.post(
            "/api/v1/auth/login",
            json={"token": "test-token", "label": "Expired"},
        )
        raw4, _ = _extract_cookie(s4_login)
        id4 = s4_login.json()["session"]["id"]
        expired_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        with sqlite3.connect(app.state.settings.db_path) as conn:
            conn.execute("UPDATE sessions SET expires_at = ? WHERE id = ?", (expired_ts, id4))
            conn.commit()
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw4))).status_code == 401
        with sqlite3.connect(app.state.settings.db_path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (id4,)).fetchone()[0]
        assert remaining == 0


async def test_user_management_password_change_and_cascade_delete(authed):
    _client, app = authed
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as clean:
        reg = await clean.post(
            "/api/v1/auth/register",
            json={"username": "ada", "password": _pw("old-1"), "token": "test-token"},
        )
        raw1, _ = _extract_cookie(reg)
        user_id = reg.json()["user"]["id"]

        login2 = await clean.post(
            "/api/v1/auth/login",
            json={"username": "ada", "password": _pw("old-1")},
        )
        raw2, _ = _extract_cookie(login2)

        users_res = await clean.get("/api/v1/auth/users", headers=_cookie_headers(raw1))
        assert users_res.status_code == 200
        users = users_res.json()["users"]
        assert len(users) == 1
        assert users[0]["active_sessions"] == 2
        assert "password_hash" not in users[0]

        # Wrong current password -> 401.
        bad_pw = await clean.post(
            "/api/v1/auth/password",
            headers=_cookie_headers(raw1),
            json={"current_password": _pw("wrong"), "new_password": _pw("new-2")},
        )
        assert bad_pw.status_code == 401

        # Token-only session cannot change password -> 400.
        tok_login = await clean.post("/api/v1/auth/login", json={"token": "test-token"})
        raw_tok, _ = _extract_cookie(tok_login)
        no_user_pw = await clean.post(
            "/api/v1/auth/password",
            headers=_cookie_headers(raw_tok),
            json={"current_password": _pw("token-only"), "new_password": _pw("new-2")},
        )
        assert no_user_pw.status_code == 400

        # Valid change with revoke_others revokes raw2 while keeping raw1.
        changed = await clean.post(
            "/api/v1/auth/password",
            headers=_cookie_headers(raw1),
            json={
                "current_password": _pw("old-1"),
                "new_password": _pw("new-2"),
                "revoke_others": True,
            },
        )
        assert changed.status_code == 200
        assert changed.json() == {"ok": True, "revoked_count": 1}
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw2))).status_code == 401
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw1))).status_code == 200

        # Old password fails, new password succeeds.
        assert (
            await clean.post(
                "/api/v1/auth/login",
                json={"username": "ada", "password": _pw("old-1")},
            )
        ).status_code == 401
        assert (
            await clean.post(
                "/api/v1/auth/login",
                json={"username": "ada", "password": _pw("new-2")},
            )
        ).status_code == 200

        # Deleting the user cascades to delete all of their sessions.
        deleted = await clean.delete(f"/api/v1/auth/users/{user_id}", headers=_cookie_headers(raw1))
        assert deleted.status_code == 200
        assert (await clean.get("/api/v1/channels", headers=_cookie_headers(raw1))).status_code == 401
        with sqlite3.connect(app.state.settings.db_path) as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
        assert cnt == 0


async def test_relay_tokens_blocked_and_origin_host_guard_enforced(authed, monkeypatch, tmp_path):
    client, _app = authed
    admin_headers = {"Authorization": "Bearer test-token"}

    # Relay token gets 403 on auth endpoints.
    relay_created = await client.post(
        "/api/v1/relays",
        headers=admin_headers,
        json={"actor": "ops", "name": "dc", "platform": "discord"},
    )
    assert relay_created.status_code == 201
    relay_token = relay_created.json()["token"]
    for path in ("/api/v1/auth/status", "/api/v1/auth/sessions", "/api/v1/auth/users"):
        res = await client.get(path, headers={"Authorization": f"Bearer {relay_token}"})
        assert res.status_code == 403

    # Cross-origin write on /api/v1/auth/login -> 403.
    cross = await client.post(
        "/api/v1/auth/login",
        headers={"Origin": "http://evil.example"},
        json={"token": "test-token"},
    )
    assert cross.status_code == 403

    # Untrusted Host -> 421.
    bad_host = await client.get("/api/v1/auth/status", headers={"Host": "attacker.example:8787"})
    assert bad_host.status_code == 421

    # Same-origin Origin matching an allowed CAT_FLEET_ALLOWED_HOSTS host is accepted.
    monkeypatch.setenv("CAT_FLEET_ALLOWED_HOSTS", "chat.lan")
    lan_settings = Settings(
        host="127.0.0.1",
        port=8787,
        db_path=str(tmp_path / "lan.sqlite"),
        token="test-token",
        dev_origins=("http://127.0.0.1:3001",),
        webhooks_enabled=False,
    )
    async for lan_client, _lan_app in _open(lan_settings):
        lan_login = await lan_client.post(
            "/api/v1/auth/login",
            headers={"Host": "chat.lan:8787", "Origin": "http://chat.lan:8787"},
            json={"token": "test-token"},
        )
        assert lan_login.status_code == 200


async def test_auth_writes_never_wake_waiters(running):
    _client, app = running
    hub = app.state.hub

    async def wait_notify():
        async with hub.waiters.condition:
            await hub.waiters.condition.wait()

    wait_task = asyncio.create_task(wait_notify())
    await asyncio.sleep(0.05)

    reg = await hub.register_user("ada", _pw("1"))
    login = await hub.login_with_password("ada", _pw("1"))
    assert login is not None
    sid = login["session"]["id"]

    # Force last_seen_at into the past so resolve_session writes a touch.
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    with sqlite3.connect(app.state.settings.db_path) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (old_ts, sid))
        conn.commit()
    resolved = await hub.resolve_session(login["raw_token"])
    assert resolved is not None
    await hub.logout_session(reg["session"]["id"])
    await hub.logout_session(sid)

    await asyncio.sleep(0.05)
    assert not wait_task.done()

    await hub.post_message("fleet", "ada", "real wake")
    await asyncio.wait_for(wait_task, timeout=2)
