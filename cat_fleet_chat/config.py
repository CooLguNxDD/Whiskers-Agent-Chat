"""Process configuration from the environment."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _env_optional(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


@dataclass(frozen=True)
class Settings:
    """Runtime settings. A missing token on a loopback bind is open local mode."""

    host: str
    port: int
    db_path: str
    token: str | None
    dev_origins: tuple[str, ...]

    @property
    def loopback_bind(self) -> bool:
        return is_loopback_host(self.host)


def is_loopback_host(host: str) -> bool:
    """True when ``host`` can only be reached from this machine."""
    name = host.strip().strip("[]").lower()
    if name in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def load_settings() -> Settings:
    """Read ``CAT_FLEET_*`` environment variables."""
    port_raw = _env_str("CAT_FLEET_PORT", "8787")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"CAT_FLEET_PORT must be an integer, got {port_raw!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"CAT_FLEET_PORT out of range: {port}")

    origins = _env_str(
        "CAT_FLEET_DEV_ORIGIN",
        "http://127.0.0.1:3001,http://localhost:3001",
    )
    dev_origins = tuple(part.strip().rstrip("/") for part in origins.split(",") if part.strip())
    return Settings(
        host=_env_str("CAT_FLEET_HOST", "127.0.0.1"),
        port=port,
        db_path=_env_str("CAT_FLEET_DB_PATH", "cat_fleet_chat.sqlite"),
        token=_env_optional("CAT_FLEET_TOKEN"),
        dev_origins=dev_origins,
    )


def assert_bind_safe(settings: Settings) -> None:
    """Refuse a non-loopback bind that has no shared token.

    Open mode is only acceptable when the socket itself is loopback. Binding
    ``0.0.0.0`` or a LAN address without ``CAT_FLEET_TOKEN`` would publish the
    fleet log to the network.
    """
    if not settings.loopback_bind and not settings.token:
        raise SystemExit(
            "CAT_FLEET_TOKEN is required when CAT_FLEET_HOST is not loopback "
            f"(got {settings.host!r}). Refusing to start an open service."
        )


def allowed_origins(settings: Settings) -> set[str]:
    """Browser origins that may write. The hub's own origin is always included."""
    own = {
        f"http://127.0.0.1:{settings.port}",
        f"http://localhost:{settings.port}",
    }
    if settings.host not in {"0.0.0.0", "::", ""}:
        own.add(f"http://{settings.host}:{settings.port}")
    return {item.rstrip("/") for item in (*own, *settings.dev_origins)}


def allowed_hostnames(settings: Settings) -> set[str]:
    """Hostnames a browser Host header may use against this process."""
    names = {"localhost", "127.0.0.1", "::1", "testserver", settings.host.lower()}
    # The documented Whiskers container connects to the host through this alias.
    if settings.token:
        names.add("host.docker.internal")
    extra = os.environ.get("CAT_FLEET_ALLOWED_HOSTS", "")
    for part in extra.split(","):
        item = part.strip().strip("[]").lower()
        if item:
            names.add(item)
    return names
