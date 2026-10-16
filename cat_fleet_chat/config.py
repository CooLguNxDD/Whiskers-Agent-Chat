"""Process configuration from the environment."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field

EMBED_PROVIDERS = ("off", "openai", "content-parts")


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
    # Kill switch for the outbound dispatcher (CAT_FLEET_WEBHOOKS=0). Inbound
    # hooks and hook management stay available either way.
    webhooks_enabled: bool = True
    # Optional embeddings for vector / hybrid search (embed.build_embedder).
    # ``off`` keeps search BM25 only. The API key never appears in repr.
    embed_provider: str = "off"
    embed_url: str | None = None
    embed_model: str | None = None
    embed_api_key: str | None = field(default=None, repr=False)
    embed_batch: int = 16
    embed_timeout: float = 20.0
    # Handles allowed to steer proposals and the evolve pause flag with chat
    # commands (evolve.py), lower-cased. Empty means nobody may command.
    human_handles: frozenset[str] = frozenset()

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
    provider = _env_str("CAT_FLEET_EMBED_PROVIDER", "off").lower()
    if provider not in EMBED_PROVIDERS:
        raise SystemExit(
            f"CAT_FLEET_EMBED_PROVIDER must be one of {', '.join(EMBED_PROVIDERS)}, got {provider!r}"
        )
    batch_raw = _env_str("CAT_FLEET_EMBED_BATCH", "16")
    timeout_raw = _env_str("CAT_FLEET_EMBED_TIMEOUT", "20")
    try:
        embed_batch = int(batch_raw)
        embed_timeout = float(timeout_raw)
    except ValueError as exc:
        raise SystemExit(
            "CAT_FLEET_EMBED_BATCH must be an integer and CAT_FLEET_EMBED_TIMEOUT a number of seconds"
        ) from exc
    if embed_batch < 1 or embed_timeout <= 0:
        raise SystemExit("CAT_FLEET_EMBED_BATCH and CAT_FLEET_EMBED_TIMEOUT must be positive")
    embed_url = _env_optional("CAT_FLEET_EMBED_URL")
    embed_model = _env_optional("CAT_FLEET_EMBED_MODEL")
    if provider != "off" and (embed_url is None or embed_model is None):
        raise SystemExit(
            f"CAT_FLEET_EMBED_PROVIDER={provider} needs CAT_FLEET_EMBED_URL and CAT_FLEET_EMBED_MODEL"
        )
    return Settings(
        host=_env_str("CAT_FLEET_HOST", "127.0.0.1"),
        port=port,
        db_path=_env_str("CAT_FLEET_DB_PATH", "cat_fleet_chat.sqlite"),
        token=_env_optional("CAT_FLEET_TOKEN"),
        dev_origins=dev_origins,
        webhooks_enabled=_env_str("CAT_FLEET_WEBHOOKS", "1").lower() not in {"0", "false", "no", "off"},
        embed_provider=provider,
        embed_url=embed_url,
        embed_model=embed_model,
        embed_api_key=_env_optional("CAT_FLEET_EMBED_API_KEY"),
        embed_batch=embed_batch,
        embed_timeout=embed_timeout,
        human_handles=human_handles(os.environ.get("CAT_FLEET_HUMAN_HANDLES", "")),
    )


def human_handles(raw: str) -> frozenset[str]:
    """``CAT_FLEET_HUMAN_HANDLES``: comma separated, trimmed, lower-cased."""
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


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
