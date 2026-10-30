"""Hub events to a platform-neutral message model, and the typed button ids.

``line`` is the same text an outbound webhook posts (``webhooks.event_line``).
``embed`` adds a title, fields and a footer. ``card`` is an embed plus action
buttons for proposals, tasks and feed posts. Each adapter turns a
:class:`Rendered` into native output (Discord embeds and components, Slack
Block Kit, Telegram HTML and inline keyboards) and defangs mentions.

A button id is ``cf1:<verb>:<kind>:<ref>``. It carries only typed ids, never
text, so a crafted message cannot aim a click at anything but the object the
button was rendered for, and the hub still checks every action.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from cat_fleet_chat.webhooks import _clip, event_line

FORMATS = ("line", "embed", "card")
BODY_CLIP = 1_500
TITLE_CLIP = 200

# Colors are hints; adapters without colors ignore them.
COLORS = {
    "message.created": 0x5865F2,
    "task.updated": 0xF0B232,
    "proposal_created": 0x23A55A,
    "proposal_updated": 0x23A55A,
    "feed_post": 0xEB459E,
    "topic.published": 0x00A8FC,
}


@dataclass(frozen=True)
class Button:
    label: str
    custom_id: str
    style: str = "secondary"  # primary | secondary | success | danger


@dataclass(frozen=True)
class Rendered:
    """One outbound message. ``text`` is always set (the fallback for every platform)."""

    text: str
    title: str | None = None
    body: str | None = None
    fields: tuple[tuple[str, str], ...] = ()
    footer: str | None = None
    color: int | None = None
    buttons: tuple[Button, ...] = ()
    # The platform message to reply to (a thread reply, a quote), if any.
    reply_to_external: str | None = None
    event_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---- button ids --------------------------------------------------------------

BUTTON_RE = re.compile(r"^cf1:(approve|reject|claim|start|done|up|down):(proposal|task|feed):([A-Za-z0-9.]{1,40})$")
VERB_KINDS = {
    "approve": "proposal",
    "reject": "proposal",
    "claim": "task",
    "start": "task",
    "done": "task",
    "up": "feed",
    "down": "feed",
}
PROPOSAL_REF_RE = re.compile(r"^P[1-9][0-9]{0,17}$")
TASK_REF_RE = re.compile(r"^([1-9][0-9]{0,17})\.([0-9]{1,18})$")
FEED_REF_RE = re.compile(r"^[1-9][0-9]{0,17}$")


@dataclass(frozen=True)
class ButtonAction:
    verb: str
    kind: str
    ref: str

    @property
    def task(self) -> tuple[int, int]:
        """``(task_id, version)`` for a task button."""
        match = TASK_REF_RE.fullmatch(self.ref)
        assert match is not None
        return int(match.group(1)), int(match.group(2))


def button_id(verb: str, kind: str, ref: str) -> str:
    return f"cf1:{verb}:{kind}:{ref}"


def parse_button(custom_id: Any) -> ButtonAction | None:
    """The action behind a button id, or ``None`` for anything malformed or foreign."""
    if not isinstance(custom_id, str):
        return None
    match = BUTTON_RE.fullmatch(custom_id)
    if match is None:
        return None
    verb, kind, ref = match.groups()
    if VERB_KINDS[verb] != kind:
        return None
    pattern = {"proposal": PROPOSAL_REF_RE, "task": TASK_REF_RE, "feed": FEED_REF_RE}[kind]
    if not pattern.fullmatch(ref):
        return None
    return ButtonAction(verb, kind, ref)


# ---- rendering ---------------------------------------------------------------


def _one_line(text: Any, limit: int) -> str:
    return _clip(" ".join(str(text or "").split()), limit)


def _buttons(event: dict[str, Any]) -> tuple[Button, ...]:
    kind = event["kind"]
    payload = event.get("payload") or {}
    if kind in {"proposal_created", "proposal_updated"} and payload.get("status") == "open":
        pid = str(payload.get("id", ""))
        if PROPOSAL_REF_RE.fullmatch(pid):
            return (
                Button("Approve", button_id("approve", "proposal", pid), "success"),
                Button("Reject…", button_id("reject", "proposal", pid), "danger"),
            )
    if kind == "task.updated":
        task_id, version = payload.get("id"), payload.get("version")
        if isinstance(task_id, int) and isinstance(version, int):
            ref = f"{task_id}.{version}"
            status = payload.get("status")
            if status == "open":
                return (Button("Claim", button_id("claim", "task", ref), "primary"),)
            if status in {"claimed", "blocked"}:
                return (Button("Start", button_id("start", "task", ref), "primary"),)
            if status == "in_progress":
                return (Button("Done", button_id("done", "task", ref), "success"),)
    if kind == "feed_post":
        post_id = payload.get("id", event.get("entity_id"))
        if isinstance(post_id, int) and post_id > 0:
            return (
                Button("👍", button_id("up", "feed", str(post_id))),
                Button("👎", button_id("down", "feed", str(post_id))),
            )
    return ()


def _embed(event: dict[str, Any]) -> tuple[str | None, str | None, tuple[tuple[str, str], ...]]:
    kind = event["kind"]
    payload = event.get("payload") or {}
    channel = event.get("channel")
    if kind == "message.created":
        title = f"#{channel} · {payload.get('author', '?')}"
        return title, _clip(str(payload.get("text", "")), BODY_CLIP), ()
    if kind == "task.updated":
        fields = [("Status", str(payload.get("status")))]
        if payload.get("assignee"):
            fields.append(("Assignee", str(payload["assignee"])))
        if channel:
            fields.append(("Channel", f"#{channel}"))
        title = f"Task #{payload.get('id')}: {_one_line(payload.get('title'), TITLE_CLIP)}"
        return title, _clip(str(payload.get("description") or ""), BODY_CLIP) or None, tuple(fields)
    if kind in {"proposal_created", "proposal_updated"}:
        fields = [("Status", str(payload.get("status"))), ("Project", str(payload.get("project")))]
        if payload.get("weight") is not None:
            fields.append(("Weight", str(payload.get("weight"))))
        title = f"{payload.get('id')}: {_one_line(payload.get('title'), TITLE_CLIP)}"
        return title, _clip(str(payload.get("summary") or ""), BODY_CLIP) or None, tuple(fields)
    if kind == "feed_post":
        return f"Feed · {payload.get('author', '?')}", _clip(str(payload.get("body", "")), BODY_CLIP), ()
    if kind == "topic.published":
        data = json.dumps(payload.get("data"), separators=(",", ":"), default=str)
        return f"📣 {payload.get('topic')}", _clip(f"`{data}`", BODY_CLIP), (("Publisher", str(payload.get("publisher"))),)
    return None, None, ()


def render(event: dict[str, Any], fmt: str = "line", *, reply_to_external: str | None = None) -> Rendered:
    """The message a route or reply delivers for ``event`` in ``fmt``."""
    text = event_line(event)
    if fmt == "line":
        return Rendered(text=text, reply_to_external=reply_to_external, event_id=event.get("id"))
    title, body, fields = _embed(event)
    return Rendered(
        text=text,
        title=title,
        body=body,
        fields=fields,
        footer=f"cat-fleet · event {event.get('id')}",
        color=COLORS.get(event["kind"]),
        buttons=_buttons(event) if fmt == "card" else (),
        reply_to_external=reply_to_external,
        event_id=event.get("id"),
    )


def render_reply(message: dict[str, Any], *, reply_to_external: str | None = None) -> Rendered:
    """A hub message delivered back to the platform it answers: author and text, no channel prefix."""
    text = f"**{message.get('author', '?')}**: {_clip(str(message.get('text', '')), 1_800)}"
    return Rendered(text=text, reply_to_external=reply_to_external, event_id=message.get("id"))
