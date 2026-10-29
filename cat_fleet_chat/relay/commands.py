"""``/fleet`` commands, shared by every platform.

Read commands (``status``, ``tasks``, ``proposals``, ``routes``, ``help``)
follow the relay's ``read_commands`` policy: anyone in a bound channel, or
operators only. ``post`` is like typing a message, so it follows the same
policy. Commands that change the relay (``bind``, ``unbind``, ``subscribe``,
``unsubscribe``, ``thread``) need an operator. The hub stays the authority:
a relay token can only touch its own bindings and routes, and proposal
decisions still go through the hub's human-handle allowlist.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from cat_fleet_chat import pubsub
from cat_fleet_chat.errors import HubError
from cat_fleet_chat.relay.adapter import CommandCall, Reply
from cat_fleet_chat.validate import CHANNEL_RE, EVENT_KINDS

if TYPE_CHECKING:
    from cat_fleet_chat.relay.core import RelayCore

LIST_MAX = 10
TEXT_MAX = 1_800

Handler = Callable[["RelayCore", CommandCall], Awaitable[Reply]]

COMMANDS: dict[str, tuple[str, str, str]] = {
    # name: (kind, usage, help)
    "help": ("read", "help", "this list"),
    "status": ("read", "status", "relay, hub and fleet status"),
    "tasks": ("read", "tasks [channel] [status]", "open tasks"),
    "proposals": ("read", "proposals [status]", "proposals (default open)"),
    "routes": ("read", "routes", "event routes delivered here"),
    "post": ("read", "post <channel> <text>", "post into a hub channel"),
    "bind": ("write", "bind <channel> [allowed,…]", "bind this channel or thread to a hub channel"),
    "unbind": ("write", "unbind", "remove this channel's binding"),
    "subscribe": ("write", "subscribe <topic-pattern|event-kind> [line|embed|card]", "route events here"),
    "unsubscribe": ("write", "unsubscribe <route-id>", "stop a route here"),
    "thread": ("write", "thread task:<id> | channel:<name>", "open a thread bound to a task or channel"),
}


def _arg(call: CommandCall, key: str) -> str:
    value = call.args.get(key)
    return str(value).strip() if value not in (None, "") else ""


def _fail(text: str) -> Reply:
    return Reply(text, private=True, ok=False)


def _hub_error(body: Any) -> Reply:
    return _fail(f"The hub refused: {body}")


async def cmd_help(core: RelayCore, call: CommandCall) -> Reply:
    operator = core.config.is_operator(call.user_id)
    lines = [
        f"`/fleet {usage}` — {text}"
        for kind, usage, text in COMMANDS.values()
        if kind == "read" or operator
    ]
    return Reply("\n".join(lines))


async def cmd_status(core: RelayCore, call: CommandCall) -> Reply:
    config = core.config
    binding = config.binding_for(call.channel_id, call.parent_id)
    lines = [
        f"Relay **{config.name}** ({config.platform}) · config v{config.version}",
        f"{len(config.bindings)} binding(s), {len(config.active_routes())} active route(s)",
        f"This channel → #{binding['hub_channel']}" if binding else "This channel is not bound.",
    ]
    ok, agents = await core.hub.get("/api/v1/agents")
    if ok:
        lines.append(f"{len(agents.get('agents', []))} agent(s) active recently")
    ok, evolve = await core.hub.get("/api/v1/evolve/state")
    if ok and evolve.get("paused"):
        lines.append("⏸️ evolve is paused")
    lines.append(f"Delivered {core.stats['delivered']}, failed {core.stats['failed']}")
    return Reply("\n".join(lines))


async def cmd_tasks(core: RelayCore, call: CommandCall) -> Reply:
    channel = _arg(call, "channel")
    if not channel:
        binding = core.config.binding_for(call.channel_id, call.parent_id)
        channel = binding["hub_channel"] if binding else ""
    status = _arg(call, "status") or None
    ok, body = await core.hub.get("/api/v1/tasks", channel=channel or None, status=status)
    if not ok:
        return _hub_error(body)
    tasks = [t for t in body.get("tasks", []) if status or t.get("status") not in ("done", "cancelled")]
    if not tasks:
        return Reply(f"No tasks{' in #' + channel if channel else ''}.")
    lines = [
        f"#{t['id']} [{t['status']}] {t['title']}" + (f" ({t['assignee']})" if t.get("assignee") else "")
        for t in tasks[:LIST_MAX]
    ]
    more = len(tasks) - LIST_MAX
    return Reply("\n".join(lines) + (f"\n…and {more} more" if more > 0 else ""))


async def cmd_proposals(core: RelayCore, call: CommandCall) -> Reply:
    status = _arg(call, "status") or "open"
    ok, body = await core.hub.get("/api/v1/proposals", status=status)
    if not ok:
        return _hub_error(body)
    rows = body.get("proposals", [])
    if not rows:
        return Reply(f"No {status} proposals.")
    lines = [
        f"{row['id']} [{row['status']}] {row['title']}" + (f" (score {row['score']})" if "score" in row else "")
        for row in rows[:LIST_MAX]
    ]
    return Reply("\n".join(lines))


async def cmd_routes(core: RelayCore, call: CommandCall) -> Reply:
    here = [r for r in core.config.routes if r["external_id"] == call.channel_id]
    if not here:
        return Reply("No routes deliver here. `/fleet subscribe <pattern>` adds one.")
    lines = []
    for route in here:
        consumer = route.get("consumer") or {}
        what = consumer.get("topics") or consumer.get("kinds") or ["everything"]
        state = "armed" if consumer.get("armed") else "idle"
        flag = "" if route.get("enabled") else " (disabled)"
        lines.append(f"route {route['id']}: {', '.join(what)} as {route['format']} · {state}{flag}")
    return Reply("\n".join(lines))


async def cmd_post(core: RelayCore, call: CommandCall) -> Reply:
    channel = _arg(call, "channel").lstrip("#")
    text = _arg(call, "text")
    if not CHANNEL_RE.fullmatch(channel) or not text:
        return _fail("Usage: `/fleet post <channel> <text>`")
    binding = core.config.binding_for(call.channel_id, call.parent_id)
    if binding is None:
        return _fail("This channel is not bound to the fleet.")
    entry = core.config.entry(binding)
    if not (entry.any_channel or channel in entry.allowed):
        return _fail(f"#{channel} is not allowed from this channel.")
    # Deterministic, so a platform's retried interaction cannot post twice.
    digest = hashlib.sha256(f"{call.user_id}\0{channel}\0{text}".encode()).hexdigest()[:24]
    ok, body = await core.post_as(call, channel, text[:TEXT_MAX], key=f"cmd-{digest}")
    return Reply(f"Posted to #{channel}." if ok else f"Not delivered: {body}", ok=ok)


async def cmd_bind(core: RelayCore, call: CommandCall) -> Reply:
    channel = _arg(call, "channel").lstrip("#")
    if not CHANNEL_RE.fullmatch(channel):
        return _fail("Usage: `/fleet bind <channel> [allowed,…]`")
    allowed = [part.strip().lstrip("#") for part in _arg(call, "allowed").split(",") if part.strip()]
    body: dict[str, Any] = {
        "actor": core.handle(call.user_name),
        "external_id": call.channel_id,
        "hub_channel": channel,
        "allowed": [name for name in allowed if name != "*"],
        "any_channel": "*" in allowed,
        "kind": "thread" if call.parent_id else "channel",
        "parent_external_id": call.parent_id,
    }
    ok, result = await core.hub.add_binding(body)
    if not ok:
        return _hub_error(result)
    await core.reload()
    return Reply(f"Bound to #{channel}.", private=False)


async def cmd_unbind(core: RelayCore, call: CommandCall) -> Reply:
    binding = next((b for b in core.config.bindings if b["external_id"] == call.channel_id), None)
    if binding is None:
        return _fail("This channel has no binding of its own.")
    ok, result = await core.hub.delete_binding(binding["id"], core.handle(call.user_name))
    if not ok:
        return _hub_error(result)
    await core.reload()
    return Reply(f"Unbound from #{binding['hub_channel']}.", private=False)


def route_filter(what: str) -> dict[str, list[str]] | None:
    """``task.updated`` is an event kind; ``ci.*.done`` a topic pattern. ``None`` if neither."""
    if what in EVENT_KINDS:
        return {"kinds": [what]}
    try:
        return {"topics": [pubsub.pattern(what)]}
    except HubError:
        return None


async def cmd_subscribe(core: RelayCore, call: CommandCall) -> Reply:
    what = _arg(call, "what")
    fmt = _arg(call, "format") or "line"
    filters = route_filter(what)
    if filters is None or fmt not in ("line", "embed", "card"):
        return _fail("Usage: `/fleet subscribe <topic-pattern|event-kind> [line|embed|card]`")
    body = {
        "actor": core.handle(call.user_name),
        "external_id": call.channel_id,
        "format": fmt,
        # Never echo this relay's own inbound messages back.
        "exclude_authors": [core.config.prefix + "*"],
        **filters,
    }
    ok, result = await core.hub.add_route(body)
    if not ok:
        return _hub_error(result)
    await core.reload()
    return Reply(f"Route {result['route']['id']}: {what} → here as {fmt}.", private=False)


async def cmd_unsubscribe(core: RelayCore, call: CommandCall) -> Reply:
    raw = _arg(call, "route")
    if not re.fullmatch(r"[1-9][0-9]{0,17}", raw):
        return _fail("Usage: `/fleet unsubscribe <route-id>` (see `/fleet routes`)")
    route = next((r for r in core.config.routes if r["id"] == int(raw)), None)
    if route is None or route["external_id"] != call.channel_id:
        return _fail(f"Route {raw} does not deliver here.")
    ok, result = await core.hub.delete_route(route["id"], core.handle(call.user_name))
    if not ok:
        return _hub_error(result)
    await core.reload()
    return Reply(f"Route {raw} removed.", private=False)


async def cmd_thread(core: RelayCore, call: CommandCall) -> Reply:
    """Open a thread bound to a task (its updates as cards) or a hub channel (its messages)."""
    target = _arg(call, "target")
    kind, _, value = target.partition(":")
    if call.parent_id is not None:
        return _fail("Run this in a channel, not inside a thread.")
    parent = core.config.binding_for(call.channel_id)
    if parent is None:
        return _fail("Bind this channel first (`/fleet bind <channel>`).")
    actor = core.handle(call.user_name)
    if kind == "task" and re.fullmatch(r"[1-9][0-9]{0,17}", value):
        name, hub_channel = f"Task #{value}", parent["hub_channel"]
        route = {"kinds": ["task.updated"], "task_ids": [int(value)], "format": "card"}
        task_id: int | None = int(value)
    elif kind == "channel" and CHANNEL_RE.fullmatch(value.lstrip("#")):
        hub_channel = value.lstrip("#")
        name = f"#{hub_channel}"
        route = {"kinds": ["message.created"], "channels": [hub_channel], "format": "line"}
        task_id = None
    else:
        return _fail("Usage: `/fleet thread task:<id>` or `/fleet thread channel:<name>`")
    try:
        thread_id = await core.adapter.create_thread(call.channel_id, name)
    except Exception as exc:  # platform permissions are the operator's
        return _fail(f"Could not open a thread: {type(exc).__name__}")
    ok, result = await core.hub.add_binding(
        {
            "actor": actor,
            "external_id": thread_id,
            "kind": "thread",
            "parent_external_id": call.channel_id,
            "hub_channel": hub_channel,
            "task_id": task_id,
        }
    )
    if not ok:
        return _hub_error(result)
    ok, result = await core.hub.add_route(
        {"actor": actor, "external_id": thread_id, "exclude_authors": [core.config.prefix + "*"], **route}
    )
    if not ok:
        return _hub_error(result)
    await core.reload()
    return Reply(f"Opened {name}: messages there go to #{hub_channel}.", private=False)


HANDLERS: dict[str, Handler] = {
    "help": cmd_help,
    "status": cmd_status,
    "tasks": cmd_tasks,
    "proposals": cmd_proposals,
    "routes": cmd_routes,
    "post": cmd_post,
    "bind": cmd_bind,
    "unbind": cmd_unbind,
    "subscribe": cmd_subscribe,
    "unsubscribe": cmd_unsubscribe,
    "thread": cmd_thread,
}


async def dispatch(core: RelayCore, call: CommandCall) -> Reply:
    """Run a command after the relay's permission policy. Unknown names get help."""
    handler = HANDLERS.get(call.name)
    if handler is None:
        return _fail(f"Unknown command `{call.name}`. Try `/fleet help`.")
    kind = COMMANDS[call.name][0]
    config = core.config
    if kind == "write" and not config.is_operator(call.user_id):
        return _fail("Only relay operators can do that.")
    bound = config.binding_for(call.channel_id, call.parent_id) is not None
    if kind == "read" and not config.may_read(call.user_id, bound):
        return _fail("Commands are not open here.")
    return await handler(core, call)


def parse_text_command(text: str) -> CommandCall | None:
    """``/fleet tasks ops`` typed as plain text (Telegram, Slack fallbacks) to a call skeleton.

    Returns a call with empty channel/user fields; the adapter fills them in.
    """
    parts = text.strip().split(maxsplit=2)
    if not parts or parts[0].lower() not in ("/fleet", "!fleet"):
        return None
    name = parts[1].lower() if len(parts) > 1 else "help"
    rest = parts[2] if len(parts) > 2 else ""
    return CommandCall(name=name, args=text_args(name, rest), channel_id="", user_id="", user_name="")


def text_args(name: str, rest: str) -> dict[str, Any]:
    """Positional text arguments to the named options each command reads."""
    words = rest.split()
    if name == "post":
        channel, _, text = rest.strip().partition(" ")
        return {"channel": channel, "text": text.strip()}
    if name == "tasks":
        return {"channel": words[0] if words else "", "status": words[1] if len(words) > 1 else ""}
    if name == "proposals":
        return {"status": words[0] if words else ""}
    if name == "bind":
        return {"channel": words[0] if words else "", "allowed": words[1] if len(words) > 1 else ""}
    if name == "subscribe":
        return {"what": words[0] if words else "", "format": words[1] if len(words) > 1 else ""}
    if name == "unsubscribe":
        return {"route": words[0] if words else ""}
    if name == "thread":
        return {"target": words[0] if words else ""}
    return {}
