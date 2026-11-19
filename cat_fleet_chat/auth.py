"""Pure auth rules, contract loading, password hashing, token helpers, and login rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import importlib.resources
import json
import re
import secrets
import time
from collections import defaultdict
from typing import Any

from cat_fleet_chat import validate
from cat_fleet_chat.errors import HubError

AUTH_CONTRACT: dict[str, Any] = json.loads(
    importlib.resources.files("cat_fleet_chat")
    .joinpath("auth_contract.json")
    .read_text(encoding="utf-8")
)

SESSION_COOKIE: str = str(AUTH_CONTRACT["cookie"]["name"])
SESSION_PREFIX: str = str(AUTH_CONTRACT["cookie"]["token_prefix"])
SESSION_TOUCH_INTERVAL: int = int(AUTH_CONTRACT["cookie"]["touch_interval_seconds"])
SESSION_TTL_DEFAULT: int = int(AUTH_CONTRACT["session_ttl_days"]["default"])
SESSION_TTL_MIN: int = int(AUTH_CONTRACT["session_ttl_days"]["min"])
SESSION_TTL_MAX: int = int(AUTH_CONTRACT["session_ttl_days"]["max"])
LOGIN_RATE_LIMIT: int = int(AUTH_CONTRACT["rate_limit"]["max_failures"])
LOGIN_RATE_WINDOW: float = float(AUTH_CONTRACT["rate_limit"]["window_seconds"])

USERNAME_RE = re.compile(str(AUTH_CONTRACT["limits"]["username_pattern"]))
PASSWORD_MIN: int = int(AUTH_CONTRACT["limits"]["password_min"])
PASSWORD_MAX: int = int(AUTH_CONTRACT["limits"]["password_max"])
LABEL_MAX: int = int(AUTH_CONTRACT["limits"]["label_max"])
USER_AGENT_MAX: int = int(AUTH_CONTRACT["limits"]["user_agent_max"])

AUTH_METHODS: tuple[str, ...] = tuple(str(item) for item in AUTH_CONTRACT["auth_methods"])
AUTH_KINDS: tuple[str, ...] = tuple(str(item) for item in AUTH_CONTRACT["auth_kinds"])
AUTH_ROUTE_SPECS: tuple[dict[str, Any], ...] = tuple(dict(item) for item in AUTH_CONTRACT["routes"])
PUBLIC_AUTH_ROUTES: frozenset[tuple[str, str]] = frozenset(
    (str(spec["method"]), str(spec["path"])) for spec in AUTH_ROUTE_SPECS if spec.get("public")
)

UA_BROWSERS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (str(entry["label"]), tuple(str(token) for token in entry["tokens"]))
    for entry in AUTH_CONTRACT["ua_browsers"]
)
UA_OSES: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (str(entry["label"]), tuple(str(token) for token in entry["tokens"]))
    for entry in AUTH_CONTRACT["ua_oses"]
)

_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 32 * 1024 * 1024
_SCRYPT_DKLEN = 32


def new_session_token() -> str:
    """Generate a high-entropy bearer secret for the ``HttpOnly`` session cookie."""
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest stored in ``sessions.token_hash``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Hash a password with salted scrypt (stdlib only)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored scrypt hash."""
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False
    if n < 2 or r < 1 or p < 1 or not salt or not expected:
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_PASSWORD_HASH = hash_password("dummy-timing-guard-password")


def dummy_verify(password: str) -> None:
    """Spend scrypt work when a login names an unknown user."""
    verify_password(password if isinstance(password, str) else "", _DUMMY_PASSWORD_HASH)


def _fail(message: str, field: str) -> HubError:
    return HubError("validation_error", message, 400, {"field": field})


def clean_username(value: Any) -> str:
    """Normalize and validate an account username (stored lowercase)."""
    if not isinstance(value, str):
        raise _fail("username must be a string", "username")
    text = value.strip().lower()
    if not USERNAME_RE.fullmatch(text):
        raise _fail(
            "username must be 2..32 characters matching [a-z0-9][a-z0-9_-]*",
            "username",
        )
    return text


def clean_display_name(value: Any, fallback: str) -> str:
    """Validate optional display name, defaulting to ``fallback`` when empty."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return validate.handle(fallback, "display_name")
    if not isinstance(value, str):
        raise _fail("display_name must be a string", "display_name")
    return validate.handle(value.strip(), "display_name")


def clean_password(value: Any, field: str = "password") -> str:
    """Validate password length without stripping whitespace."""
    if not isinstance(value, str):
        raise _fail(f"{field} must be a string", field)
    if len(value) < PASSWORD_MIN:
        raise _fail(f"{field} must be at least {PASSWORD_MIN} characters", field)
    if len(value) > PASSWORD_MAX:
        raise _fail(f"{field} exceeds {PASSWORD_MAX} characters", field)
    return value


def _match_label(text: str, table: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    for label, tokens in table:
        if any(token in text for token in tokens):
            return label
    return None


def summarize_user_agent(ua: str) -> str:
    """Build a short human-readable device label from a User-Agent header."""
    text = ua.strip()
    if not text:
        return "Browser session"
    lower = text.lower()
    browser = _match_label(lower, UA_BROWSERS)
    if browser is None:
        return "API client"
    os_name = _match_label(lower, UA_OSES)
    return f"{browser} on {os_name}" if os_name is not None else browser


def clean_label(value: Any, user_agent: str = "") -> str:
    """Validate an optional session label or summarize the User-Agent."""
    if value is None:
        return summarize_user_agent(user_agent)[:LABEL_MAX]
    if not isinstance(value, str):
        raise _fail("label must be a string", "label")
    text = value.strip()
    if not text:
        return summarize_user_agent(user_agent)[:LABEL_MAX]
    if len(text) > LABEL_MAX:
        raise _fail(f"label exceeds {LABEL_MAX} characters", "label")
    return text


def clean_user_agent(value: str) -> str:
    """Truncate raw User-Agent strings stored alongside a session."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:USER_AGENT_MAX]


def public_user(row: dict[str, Any], *, active_sessions: int | None = None) -> dict[str, Any]:
    """Shape a user row for REST responses without exposing ``password_hash``."""
    out: dict[str, Any] = {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row.get("last_login_at"),
    }
    if active_sessions is not None:
        out["active_sessions"] = int(active_sessions)
    return out


def public_session(row: dict[str, Any], *, current_id: int | None = None) -> dict[str, Any]:
    """Shape a session row for REST responses without exposing ``token_hash``."""
    sid = int(row["id"])
    user_id = row.get("user_id")
    return {
        "id": sid,
        "user_id": int(user_id) if user_id is not None else None,
        "username": row.get("username"),
        "display_name": row.get("display_name"),
        "auth_method": row["auth_method"],
        "label": row.get("label") or "",
        "user_agent": row.get("user_agent") or "",
        "ip_address": row.get("ip_address") or "",
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
        "expires_at": row["expires_at"],
        "current": current_id is not None and sid == int(current_id),
    }


class LoginRateLimiter:
    """In-memory per-IP sliding window limiting failed sign-in attempts."""

    def __init__(
        self,
        limit: int = LOGIN_RATE_LIMIT,
        window: float = LOGIN_RATE_WINDOW,
    ) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window
        recent = [ts for ts in self._failures.get(key, ()) if ts > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def check(self, key: str) -> None:
        now = time.monotonic()
        recent = self._prune(key, now)
        if len(recent) >= self.limit:
            raise HubError(
                "rate_limited",
                "too many failed sign-in attempts; try again shortly",
                429,
            )

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        recent = self._prune(key, now)
        recent.append(now)
        self._failures[key] = recent

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
