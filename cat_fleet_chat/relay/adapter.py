"""The contract between ``RelayCore`` and a chat platform.

An adapter owns the platform client (login, events, rate limits). It turns
native events into :class:`Inbound`, :class:`CommandCall` and
:class:`ButtonPress` for the core, and posts :class:`~.render.Rendered`
messages the core hands it. Nothing platform-specific crosses this line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from cat_fleet_chat.relay.render import Rendered


@dataclass(frozen=True)
class Inbound:
    """One human message seen on the platform."""

    channel_id: str  # the platform channel (for a thread: the thread itself)
    message_id: str
    user_id: str
    user_name: str
    text: str
    parent_id: str | None = None  # the channel a thread lives in
    is_bot: bool = False
    attachments: tuple[str, ...] = ()
    raw: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class CommandCall:
    """A ``/fleet <name> …`` command."""

    name: str
    args: dict[str, Any]
    channel_id: str
    user_id: str
    user_name: str
    parent_id: str | None = None


@dataclass(frozen=True)
class ButtonPress:
    custom_id: str
    channel_id: str
    user_id: str
    user_name: str
    parent_id: str | None = None
    reason: str | None = None  # from a follow-up modal (reject)


@dataclass(frozen=True)
class Reply:
    """What the core answers a command or button with. ``private`` maps to ephemeral."""

    text: str
    private: bool = True
    ok: bool = True


class Adapter(Protocol):
    platform: str

    async def send(self, channel_id: str, message: Rendered, *, nonce: str | None = None) -> str | None:
        """Post ``message``; return the platform message id. Raise on failure (the core retries)."""

    async def create_thread(self, channel_id: str, name: str) -> str:
        """Open a thread in ``channel_id`` and return its id."""

    def status(self) -> dict[str, Any]:
        """Small, secret-free status for the heartbeat (connected, user, counts)."""
