"""The relay's view of its hub config: bindings, routes, allowlists, one ``version``.

Loaded from ``GET /api/v1/relays/{name}/config`` and reloaded when a
heartbeat reports a new version. The last good snapshot is cached on disk,
so a relay that restarts while the hub is down keeps its old config instead
of relaying nothing (or everything).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cat_fleet_chat.relay.routing import MapEntry

logger = logging.getLogger("cat_fleet_chat.relay")


@dataclass(frozen=True)
class RelayConfig:
    name: str
    platform: str
    version: int
    enabled: bool
    settings: dict[str, Any]
    bindings: tuple[dict[str, Any], ...] = ()
    routes: tuple[dict[str, Any], ...] = ()
    replies_consumer: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> RelayConfig:
        relay = snapshot["relay"]
        return cls(
            name=relay["name"],
            platform=relay["platform"],
            version=int(snapshot["version"]),
            enabled=bool(relay["enabled"]),
            settings=dict(relay.get("config") or {}),
            bindings=tuple(snapshot.get("bindings") or ()),
            routes=tuple(snapshot.get("routes") or ()),
            replies_consumer=snapshot.get("replies_consumer"),
            raw=snapshot,
        )

    # ---- lookups -------------------------------------------------------------

    def binding_for(self, channel_id: str, parent_id: str | None = None) -> dict[str, Any] | None:
        """The binding for a platform channel; a thread falls back to its parent channel's."""
        for binding in self.bindings:
            if binding["external_id"] == channel_id:
                return binding
        if parent_id is not None:
            for binding in self.bindings:
                if binding["external_id"] == parent_id and binding["kind"] == "channel":
                    return binding
        return None

    @staticmethod
    def entry(binding: dict[str, Any]) -> MapEntry:
        """The ``#channel`` targeting rules for a binding."""
        return MapEntry(
            binding["hub_channel"],
            frozenset([binding["hub_channel"], *binding.get("allowed", [])]),
            bool(binding.get("any_channel")),
        )

    @property
    def prefix(self) -> str:
        return self.settings.get("prefix") or "dc-"

    def is_operator(self, user_id: str) -> bool:
        return str(user_id) in self.settings.get("operators", [])

    def is_action_user(self, user_id: str) -> bool:
        return str(user_id) in self.settings.get("action_users", []) or self.is_operator(user_id)

    def may_read(self, user_id: str, bound: bool) -> bool:
        """Read commands: anyone in a bound channel, or operators only, per ``read_commands``."""
        if self.is_operator(user_id):
            return True
        return bound and self.settings.get("read_commands", "bound") == "bound"

    def active_routes(self) -> list[dict[str, Any]]:
        return [
            route for route in self.routes if route.get("enabled") and route.get("consumer") is not None
        ]


class ConfigCache:
    """The last good snapshot on disk. Written atomically; unreadable state is ignored."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None

    def load(self) -> dict[str, Any] | None:
        if self.path is None:
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            RelayConfig.from_snapshot(data)
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("ignoring unreadable relay config cache %s: %r", self.path, exc)
            return None

    def save(self, snapshot: dict[str, Any]) -> None:
        if self.path is None:
            return
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = stream.name
                json.dump(snapshot, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            logger.warning("could not cache relay config: %r", exc)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
