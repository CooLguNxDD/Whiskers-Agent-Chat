"""Slack adapter for ``cat-fleet-relay`` over Socket Mode (no public URL needed).

Needs ``pip install "cat-fleet-chat[slack]"``, a bot token (``xoxb-``) with
``chat:write``, ``channels:history``, ``reactions:write``, ``users:read`` and
``commands``, an app-level token (``xapp-``) with ``connections:write``, and
a ``/fleet`` slash command with interactivity enabled.

Ids: a channel is ``C0123``; a thread is ``C0123:<thread_ts>`` and reports
``C0123`` as its parent, so a thread inherits its channel's binding. Buttons
are Block Kit buttons whose ``action_id`` is the ``cf1:`` id; reject opens a
modal for the reason. Command answers marked private are ephemeral.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from cat_fleet_chat.relay.adapter import ButtonPress, CommandCall, Inbound, Reply
from cat_fleet_chat.relay.commands import parse_text_command
from cat_fleet_chat.relay.render import Rendered, parse_button

logger = logging.getLogger("cat_fleet_chat.relay.slack")

TEXT_MAX = 3_000
OK_MARK = "white_check_mark"
FAIL_MARK = "x"
STYLES = {"primary": "primary", "success": "primary", "danger": "danger"}


def slack_text(text: str) -> str:
    """Markdown-ish hub text to Slack mrkdwn, with broadcasts and user pings defanged."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return text.replace("<!", "<​!").replace("<@", "<​@")[:TEXT_MAX]


def split_id(external_id: str) -> tuple[str, str | None]:
    """``C0123:1700.0001`` to ``("C0123", "1700.0001")``."""
    channel, _, thread_ts = external_id.partition(":")
    return channel, thread_ts or None


def blocks(message: Rendered) -> list[dict[str, Any]]:
    """Block Kit for an embed or card. A line message needs none."""
    out: list[dict[str, Any]] = []
    if message.title:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": slack_text(f"*{message.title}*")}})
    if message.body:
        out.append({"type": "section", "text": {"type": "mrkdwn", "text": slack_text(message.body)}})
    if message.fields:
        out.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": slack_text(f"*{name}*\n{value}")} for name, value in message.fields[:10]
                ],
            }
        )
    if message.buttons:
        elements = []
        for button in message.buttons:
            element: dict[str, Any] = {
                "type": "button",
                "text": {"type": "plain_text", "text": button.label[:75], "emoji": True},
                "action_id": button.custom_id,
                "value": button.custom_id,
            }
            if button.style in STYLES:
                element["style"] = STYLES[button.style]
            elements.append(element)
        out.append({"type": "actions", "elements": elements})
    if message.footer:
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": slack_text(message.footer)}]})
    return out


def reject_modal(custom_id: str, channel_id: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": custom_id,
        "private_metadata": channel_id,
        "title": {"type": "plain_text", "text": "Reject proposal"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason",
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {"type": "plain_text_input", "action_id": "reason", "multiline": True, "max_length": 500},
            }
        ],
    }


class SlackAdapter:
    platform = "slack"

    def __init__(self, web: Any) -> None:
        self.web = web
        self.core: Any = None
        self._names: dict[str, str] = {}
        self.bot_user: str | None = None
        self.connected = False

    def attach(self, core: Any) -> None:
        self.core = core

    # ---- outbound ----------------------------------------------------------------

    async def send(self, channel_id: str, message: Rendered, *, nonce: str | None = None) -> str | None:
        channel, thread_ts = split_id(channel_id)
        kwargs: dict[str, Any] = {"channel": channel, "text": slack_text(message.text), "unfurl_links": False}
        built = blocks(message) if message.title else []
        if built:
            kwargs["blocks"] = built
        thread = thread_ts or message.reply_to_external
        if thread:
            kwargs["thread_ts"] = thread
        response = await self.web.chat_postMessage(**kwargs)
        return str(response["ts"])

    async def create_thread(self, channel_id: str, name: str) -> str:
        channel, _ = split_id(channel_id)
        response = await self.web.chat_postMessage(channel=channel, text=f"🧵 {slack_text(name)}")
        return f"{channel}:{response['ts']}"

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "user": self.bot_user}

    # ---- helpers ---------------------------------------------------------------------

    async def user_name(self, user_id: str) -> str:
        if user_id not in self._names:
            try:
                info = await self.web.users_info(user=user_id)
                profile = info["user"].get("profile", {})
                self._names[user_id] = profile.get("display_name") or info["user"].get("name") or user_id
            except Exception:  # noqa: BLE001 - a name is cosmetic
                return user_id
        return self._names[user_id]

    async def _reply(self, channel_id: str, user_id: str, reply: Reply, thread_ts: str | None = None) -> None:
        text = slack_text(reply.text)
        try:
            if reply.private:
                await self.web.chat_postEphemeral(channel=channel_id, user=user_id, text=text, thread_ts=thread_ts)
            else:
                await self.web.chat_postMessage(channel=channel_id, text=text, thread_ts=thread_ts)
        except Exception as exc:  # noqa: BLE001 - Slack permissions are the operator's
            logger.warning("could not answer in %s: %r", channel_id, exc)

    # ---- inbound ------------------------------------------------------------------------

    async def on_event(self, event: dict[str, Any]) -> str:
        """A Socket Mode ``events_api`` event. Only plain human messages relay."""
        if event.get("type") != "message" or event.get("subtype") not in (None, "file_share", "thread_broadcast"):
            return "ignored"
        if event.get("bot_id") or not event.get("user") or event.get("user") == self.bot_user:
            return "ignored"
        channel = event["channel"]
        ts = event["ts"]
        thread_ts = event.get("thread_ts")
        in_thread = bool(thread_ts and thread_ts != ts)
        files = tuple(f.get("permalink", "") for f in event.get("files", []) if f.get("permalink"))
        inbound = Inbound(
            channel_id=f"{channel}:{thread_ts}" if in_thread else channel,
            message_id=ts,
            user_id=event["user"],
            user_name=await self.user_name(event["user"]),
            text=event.get("text", ""),
            parent_id=channel if in_thread else None,
            attachments=files,
        )
        result = await self.core.inbound(inbound)
        config = self.core.config
        if result.status != "ignored" and config is not None and config.settings.get("status_marks", True):
            try:
                await self.web.reactions_add(
                    channel=channel, timestamp=ts, name=OK_MARK if result.status == "sent" else FAIL_MARK
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not mark message: %r", exc)
        if result.status == "failed":
            await self._reply(channel, inbound.user_id, Reply(f"Not delivered to the fleet: {result.reason}"), thread_ts or ts)
        elif result.note:
            await self._reply(channel, inbound.user_id, Reply(result.note), thread_ts or ts)
        return result.status

    async def on_command(self, payload: dict[str, Any]) -> Reply:
        """The ``/fleet`` slash command: ``text`` is everything after it."""
        skeleton = parse_text_command(f"/fleet {payload.get('text', '')}")
        user_id = payload["user_id"]
        channel_id = payload["channel_id"]
        call = CommandCall(
            name=skeleton.name if skeleton else "help",
            args=skeleton.args if skeleton else {},
            channel_id=channel_id,
            user_id=user_id,
            user_name=payload.get("user_name") or await self.user_name(user_id),
        )
        reply = await self.core.command(call)
        await self._reply(channel_id, user_id, reply)
        return reply

    async def on_interactive(self, payload: dict[str, Any]) -> Reply | None:
        """``block_actions`` (card buttons) and ``view_submission`` (the reject reason)."""
        user_id = payload["user"]["id"]
        user_name = payload["user"].get("username") or payload["user"].get("name") or user_id
        if payload.get("type") == "view_submission":
            view = payload["view"]
            custom_id = view.get("callback_id")
            channel_id = view.get("private_metadata", "")
            reason = (((view.get("state") or {}).get("values") or {}).get("reason") or {}).get("reason", {}).get("value")
        elif payload.get("type") == "block_actions":
            actions = payload.get("actions") or []
            custom_id = actions[0].get("action_id") if actions else None
            channel_id = (payload.get("channel") or {}).get("id", "")
            thread_ts = (payload.get("message") or {}).get("thread_ts")
            if thread_ts:
                channel_id = f"{channel_id}:{thread_ts}"
            reason = None
        else:
            return None
        action = parse_button(custom_id)
        if action is None:
            return None
        channel, thread_ts = split_id(channel_id)
        parent = channel if thread_ts else None
        if action.verb == "reject" and reason is None:
            if not self.core.config or not self.core.config.is_action_user(user_id):
                reply = Reply("You are not allowed to act on fleet cards.", ok=False)
                await self._reply(channel, user_id, reply, thread_ts)
                return reply
            await self.web.views_open(trigger_id=payload["trigger_id"], view=reject_modal(custom_id, channel_id))
            return None
        reply = await self.core.button(ButtonPress(custom_id, channel_id, user_id, user_name, parent, reason))
        await self._reply(channel, user_id, reply, thread_ts)
        return reply

    async def dispatch(self, kind: str, payload: dict[str, Any]) -> None:
        """Route one Socket Mode request. Never raises into the socket client."""
        try:
            if kind == "events_api":
                await self.on_event(payload.get("event") or {})
            elif kind == "slash_commands":
                await self.on_command(payload)
            elif kind == "interactive":
                await self.on_interactive(payload)
        except Exception:  # noqa: BLE001
            logger.exception("slack %s handler failed", kind)


def run(core_factory: Any, bot_token: str, app_token: str) -> None:
    """Connect over Socket Mode and run until interrupted."""
    if not bot_token.startswith("xoxb-") or not app_token.startswith("xapp-"):
        raise SystemExit("cat-fleet-relay --platform slack needs SLACK_BOT_TOKEN (xoxb-) and SLACK_APP_TOKEN (xapp-)")
    try:
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError:
        raise SystemExit("cat-fleet-relay --platform slack needs slack_sdk: pip install 'cat-fleet-chat[slack]'") from None

    async def main() -> None:
        web = AsyncWebClient(token=bot_token)
        adapter = SlackAdapter(web)
        core = core_factory(adapter)
        adapter.attach(core)
        auth = await web.auth_test()
        adapter.bot_user = auth.get("user_id")
        await core.start()
        socket = SocketModeClient(app_token=app_token, web_client=web)
        running: set[asyncio.Task] = set()

        async def listener(client, request) -> None:
            # Ack first: Slack retries anything not acked within 3 seconds.
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            task = asyncio.create_task(adapter.dispatch(request.type, request.payload or {}))
            running.add(task)  # a strong reference until it finishes
            task.add_done_callback(running.discard)

        socket.socket_mode_request_listeners.append(listener)
        await socket.connect()
        adapter.connected = True
        try:
            await asyncio.Event().wait()
        finally:
            adapter.connected = False
            await socket.close()
            await core.stop()
            await core.hub.client.aclose()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
