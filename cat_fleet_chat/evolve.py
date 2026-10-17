"""Pure helpers for human evolve commands: the chat grammar and ack texts.

No I/O lives here. ``hub.py`` decides who may command and applies the result.

Grammar (first non-empty line of a message, verb case-insensitive, an optional
leading ``/`` or ``!``)::

    approve P<n>
    reject P<n> <reason...>
    edit P<n> <field> <value...>
    pause evolve
    resume evolve

``P<n>`` may also be written ``p<n>`` or plain ``<n>``. Anything that does not
match exactly is not a command: no fuzzy matching, so a sentence that merely
mentions "approve" stays an ordinary message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cat_fleet_chat.proposals import EDITABLE_FIELDS, public_id

# Author of every ack. Messages by this author are never parsed as commands.
ACK_AUTHOR = "evolve"

VERBS = ("approve", "reject", "edit", "pause", "resume")

# ASCII digits only, the same shape ``proposals.proposal_number`` accepts.
_ID = r"[Pp]?([1-9][0-9]{0,17})"
_PREFIX = r"[/!]?"
_APPROVE = re.compile(rf"{_PREFIX}approve[ \t]+{_ID}", re.IGNORECASE)
_REJECT = re.compile(rf"{_PREFIX}reject[ \t]+{_ID}[ \t]+(\S.*)", re.IGNORECASE)
_EDIT = re.compile(rf"{_PREFIX}edit[ \t]+{_ID}[ \t]+([A-Za-z_]+)[ \t]+(\S.*)", re.IGNORECASE)
_EVOLVE = re.compile(rf"{_PREFIX}(pause|resume)[ \t]+evolve", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    """One parsed command. ``number`` is the proposal row id (``P<number>``)."""

    verb: str
    number: int | None = None
    field: str | None = None
    value: str | None = None
    reason: str | None = None

    @property
    def proposal_id(self) -> str | None:
        return public_id(self.number) if self.number is not None else None


def first_line(text: str) -> str:
    """The first non-empty line, stripped. Empty when there is none."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def parse_command(text: object) -> Command | None:
    """The command in ``text``, or ``None`` when it is not exactly one."""
    if not isinstance(text, str):
        return None
    line = first_line(text)
    if not line:
        return None
    if match := _APPROVE.fullmatch(line):
        return Command("approve", number=int(match.group(1)))
    if match := _REJECT.fullmatch(line):
        return Command("reject", number=int(match.group(1)), reason=match.group(2).strip())
    if match := _EDIT.fullmatch(line):
        field = match.group(2).lower()
        if field not in EDITABLE_FIELDS:
            return None
        return Command("edit", number=int(match.group(1)), field=field, value=match.group(3).strip())
    if match := _EVOLVE.fullmatch(line):
        return Command(match.group(1).lower())
    return None


def ack_text(command: Command, result: dict) -> str:
    """The ack body for a command the hub applied successfully."""
    pid = command.proposal_id
    if command.verb == "approve":
        return f"approved {pid}"
    if command.verb == "reject":
        return f"rejected {pid}: {command.reason}"
    if command.verb == "edit":
        if result.get("idempotent"):
            return f"{pid} {command.field} unchanged"
        return f"{pid} {command.field} updated"
    state = "paused" if command.verb == "pause" else "resumed"
    return f"evolve already {state}" if result.get("idempotent") else f"evolve {state}"


def error_text(command: Command, code: str, message: str) -> str:
    """The ack body when the hub refused the command."""
    pid = command.proposal_id
    if code == "not_found" and pid is not None:
        return f"{pid} not found"
    where = f"{command.verb} {pid}" if pid is not None else f"{command.verb} evolve"
    return f"{where} failed: {message}"
