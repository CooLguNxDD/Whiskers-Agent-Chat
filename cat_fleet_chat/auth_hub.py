"""Business rules for portal user accounts and durable SQLite sessions.

A mixin of :class:`cat_fleet_chat.hub.Hub`. All database writes go through
``self._quiet`` so signing in, refreshing ``last_seen_at``, or logging out
never wakes SSE or notification waiters.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from cat_fleet_chat import auth, store
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.validate import HANDLE_RE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _user_from_session_row(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("user_id") is None:
        return None
    return auth.public_user(
        {
            "id": row["user_id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "created_at": row["user_created_at"],
            "updated_at": row["user_updated_at"],
            "last_login_at": row["user_last_login_at"],
        }
    )


class AuthHubMixin:
    """User accounts and SQLite session lifecycle. Expects ``self.db`` and ``self._quiet``."""

    async def auth_has_users(self) -> bool:
        count = await self.db.read(store.count_users)
        return count > 0

    async def register_user(
        self,
        username_value: Any,
        password_value: Any,
        *,
        display_name_value: Any = None,
        create_session: bool = True,
        label_value: Any = None,
        user_agent: str = "",
        ip_address: str = "",
        ttl_days: int = auth.SESSION_TTL_DEFAULT,
    ) -> dict[str, Any]:
        username = auth.clean_username(username_value)
        raw_trimmed = username_value.strip() if isinstance(username_value, str) else username
        fallback_display = raw_trimmed if HANDLE_RE.fullmatch(raw_trimmed) else username
        display_name = auth.clean_display_name(display_name_value, fallback_display)
        password = auth.clean_password(password_value, "password")
        ua = auth.clean_user_agent(user_agent)
        label = auth.clean_label(label_value, ua)
        password_hash = await asyncio.to_thread(auth.hash_password, password)

        now = _now()
        now_iso = _iso(now)
        expires_iso = _iso(now + timedelta(days=ttl_days))
        raw_token = auth.new_session_token() if create_session else None
        token_hash = auth.hash_token(raw_token) if raw_token is not None else None

        async def op(conn):
            existing = await store.get_user_by_username(conn, username)
            if existing is not None:
                raise HubError(
                    "conflict",
                    f"username {username!r} already exists",
                    409,
                    {"field": "username"},
                )
            try:
                user_id = await store.insert_user(
                    conn,
                    {
                        "username": username,
                        "display_name": display_name,
                        "password_hash": password_hash,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                        "last_login_at": now_iso if create_session else None,
                    },
                )
            except sqlite3.IntegrityError as exc:
                raise HubError(
                    "conflict",
                    f"username {username!r} already exists",
                    409,
                    {"field": "username"},
                ) from exc
            session_row = None
            if create_session and token_hash is not None:
                await store.prune_expired_sessions(conn, now_iso)
                session_id = await store.insert_session(
                    conn,
                    {
                        "token_hash": token_hash,
                        "user_id": user_id,
                        "auth_method": "password",
                        "label": label,
                        "user_agent": ua,
                        "ip_address": ip_address,
                        "created_at": now_iso,
                        "last_seen_at": now_iso,
                        "expires_at": expires_iso,
                    },
                )
                session_row = await store.get_session_by_id(conn, session_id)
            user_row = await store.get_user_by_id(conn, user_id)
            return user_row, session_row

        user_row, session_row = await self._quiet(op)
        return {
            "user": auth.public_user(user_row),
            "session": (
                auth.public_session(session_row, current_id=session_row["id"])
                if session_row is not None
                else None
            ),
            "raw_token": raw_token,
        }

    async def login_with_password(
        self,
        username_value: Any,
        password_value: Any,
        *,
        label_value: Any = None,
        user_agent: str = "",
        ip_address: str = "",
        ttl_days: int = auth.SESSION_TTL_DEFAULT,
    ) -> dict[str, Any] | None:
        ua = auth.clean_user_agent(user_agent)
        label = auth.clean_label(label_value, ua)
        password_text = password_value if isinstance(password_value, str) else ""
        if not isinstance(username_value, str) or not password_text:
            await asyncio.to_thread(auth.dummy_verify, password_text)
            return None
        username = username_value.strip().lower()
        if not auth.USERNAME_RE.fullmatch(username):
            await asyncio.to_thread(auth.dummy_verify, password_text)
            return None

        user = await self.db.read(lambda conn: store.get_user_by_username(conn, username))
        if user is None:
            await asyncio.to_thread(auth.dummy_verify, password_text)
            return None
        verified = await asyncio.to_thread(auth.verify_password, password_text, user["password_hash"])
        if not verified:
            return None

        now = _now()
        now_iso = _iso(now)
        expires_iso = _iso(now + timedelta(days=ttl_days))
        raw_token = auth.new_session_token()
        token_hash = auth.hash_token(raw_token)
        user_id = int(user["id"])

        async def op(conn):
            await store.prune_expired_sessions(conn, now_iso)
            await store.touch_user_login(conn, user_id, now_iso)
            session_id = await store.insert_session(
                conn,
                {
                    "token_hash": token_hash,
                    "user_id": user_id,
                    "auth_method": "password",
                    "label": label,
                    "user_agent": ua,
                    "ip_address": ip_address,
                    "created_at": now_iso,
                    "last_seen_at": now_iso,
                    "expires_at": expires_iso,
                },
            )
            user_row = await store.get_user_by_id(conn, user_id)
            session_row = await store.get_session_by_id(conn, session_id)
            return user_row, session_row

        user_row, session_row = await self._quiet(op)
        return {
            "user": auth.public_user(user_row),
            "session": auth.public_session(session_row, current_id=session_row["id"]),
            "raw_token": raw_token,
        }

    async def login_with_token(
        self,
        *,
        label_value: Any = None,
        user_agent: str = "",
        ip_address: str = "",
        ttl_days: int = auth.SESSION_TTL_DEFAULT,
    ) -> dict[str, Any]:
        ua = auth.clean_user_agent(user_agent)
        label = auth.clean_label(label_value, ua)
        now = _now()
        now_iso = _iso(now)
        expires_iso = _iso(now + timedelta(days=ttl_days))
        raw_token = auth.new_session_token()
        token_hash = auth.hash_token(raw_token)

        async def op(conn):
            await store.prune_expired_sessions(conn, now_iso)
            session_id = await store.insert_session(
                conn,
                {
                    "token_hash": token_hash,
                    "user_id": None,
                    "auth_method": "token",
                    "label": label,
                    "user_agent": ua,
                    "ip_address": ip_address,
                    "created_at": now_iso,
                    "last_seen_at": now_iso,
                    "expires_at": expires_iso,
                },
            )
            return await store.get_session_by_id(conn, session_id)

        session_row = await self._quiet(op)
        return {
            "user": None,
            "session": auth.public_session(session_row, current_id=session_row["id"]),
            "raw_token": raw_token,
        }

    async def resolve_session(
        self, raw_token: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        if not isinstance(raw_token, str) or not raw_token.startswith(auth.SESSION_PREFIX):
            return None
        digest = auth.hash_token(raw_token)
        row = await self.db.read(lambda conn: store.get_session_by_token_hash(conn, digest))
        if row is None:
            return None
        now = _now()
        now_iso = _iso(now)
        try:
            expires_at = _parse_ts(row["expires_at"])
        except ValueError:
            expires_at = now
        if expires_at <= now:
            sid = int(row["id"])
            await self._quiet(lambda conn: store.delete_session(conn, sid))
            return None

        should_touch = False
        try:
            last_seen = _parse_ts(row["last_seen_at"])
            should_touch = (now - last_seen).total_seconds() >= auth.SESSION_TOUCH_INTERVAL
        except ValueError:
            should_touch = True
        if should_touch:
            sid = int(row["id"])
            await self._quiet(lambda conn: store.touch_session(conn, sid, now_iso))
            row["last_seen_at"] = now_iso

        return (
            auth.public_session(row, current_id=int(row["id"])),
            _user_from_session_row(row),
        )

    async def logout_session(self, session_id: int) -> bool:
        deleted = await self._quiet(lambda conn: store.delete_session(conn, session_id))
        return deleted > 0

    async def list_sessions(self, *, current_session_id: int | None = None) -> dict[str, Any]:
        now_iso = _iso(_now())
        rows = await self.db.read(lambda conn: store.list_sessions(conn, now_iso))
        return {
            "sessions": [auth.public_session(row, current_id=current_session_id) for row in rows]
        }

    async def revoke_session(self, session_id: int) -> dict[str, Any]:
        deleted = await self._quiet(lambda conn: store.delete_session(conn, session_id))
        if deleted == 0:
            raise HubError("not_found", "session not found", 404)
        return {"revoked": True, "id": int(session_id)}

    async def revoke_other_sessions(self, keep_session_id: int | None) -> dict[str, Any]:
        count = await self._quiet(lambda conn: store.delete_other_sessions(conn, keep_session_id))
        return {"revoked_count": int(count)}

    async def list_users(self) -> dict[str, Any]:
        now_iso = _iso(_now())
        rows = await self.db.read(lambda conn: store.list_users(conn, now_iso))
        return {
            "users": [
                auth.public_user(row, active_sessions=int(row["active_sessions"])) for row in rows
            ]
        }

    async def change_password(
        self,
        user_id: int,
        current_password: Any,
        new_password: Any,
        *,
        keep_session_id: int | None = None,
        revoke_others: bool = False,
    ) -> dict[str, Any]:
        new_pw = auth.clean_password(new_password, "new_password")
        user = await self.db.read(lambda conn: store.get_user_by_id(conn, user_id))
        if user is None:
            raise HubError("not_found", "user not found", 404)
        ok = isinstance(current_password, str) and await asyncio.to_thread(
            auth.verify_password, current_password, user["password_hash"]
        )
        if not ok:
            raise HubError("unauthorized", "current password is incorrect", 401)
        new_hash = await asyncio.to_thread(auth.hash_password, new_pw)
        now_iso = _iso(_now())

        async def op(conn):
            await store.update_user_password(conn, user_id, new_hash, now_iso)
            if revoke_others:
                return await store.delete_user_other_sessions(conn, user_id, keep_session_id)
            return 0

        revoked = await self._quiet(op)
        return {"ok": True, "revoked_count": int(revoked)}

    async def delete_user(self, user_id: int) -> dict[str, Any]:
        deleted = await self._quiet(lambda conn: store.delete_user(conn, user_id))
        if deleted == 0:
            raise HubError("not_found", "user not found", 404)
        return {"deleted": True, "id": int(user_id)}
