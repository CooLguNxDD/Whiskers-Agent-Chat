"""Telegram adapter for ``cat-fleet-relay``: the Bot API over httpx long polling.

No extra dependency and no public URL. Create a bot with @BotFather, add it to
the group, and turn its privacy mode off (or make it an admin) so it sees
every message. Ids: a chat is ``-100123``; a forum topic is ``-100123:7`` and
reports the chat as its parent.

Commands are ``/fleet <name> …`` messages (``/fleet@yourbot`` too); Telegram
has no ephemeral answers, so command replies go to the chat. Buttons are
inline keyboards whose ``callback_data`` is the ``cf1:`` id; rejecting asks
for the reason with a forced reply, since Telegram has no modals.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import httpx

from cat_fleet_chat.relay.adapter import ButtonPress, CommandCall, Inbound, Reply
from cat_fleet_chat.relay.commands import parse_text_command
from cat_fleet_chat.relay.render import Rendered, parse_button

logger = logging.getLogger("cat_fleet_chat.relay.telegram")

API = "https://api.telegram.org"
POLL_TIMEOUT = 30
TEXT_MAX = 4_000
PENDING_MAX = 200
REASON_PROMPT = "Reply to this message with the reason to reject {ref}."


def telegram_html(text: str) -> str:
    """Hub text to Telegram HTML: escaped, ``**bold**`` kept, ``@`` pings defanged."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return escaped.replace("@", "@​")[:TEXT_MAX]


def split_id(external_id: str) -> tuple[str, int | None]:
    chat, _, topic = external_id.partition(":")
    return chat, int(topic) if topic.isdigit() else None


def message_html(message: Rendered) -> str:
    if not message.title:
        return telegram_html(message.text)
    parts = [f"<b>{telegram_html(message.title)}</b>"]
    if message.body:
        parts.append(telegram_html(message.body))
    parts.extend(f"<b>{telegram_html(name)}</b>: {telegram_html(value)}" for name, value in message.fields)
    if message.footer:
        parts.append(f"<i>{telegram_html(message.footer)}</i>")
    return "\n".join(parts)[:TEXT_MAX]


class TelegramError(RuntimeError):
    pass


class TelegramAdapter:
    platform = "telegram"

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client = client
        self.base = f"{API}/bot{token}"
        self.core: Any = None
        self.bot_id: int | None = None
        self.bot_username: str | None = None
        self.offset = 0
        self.connected = False
        # Forced-reply prompt (chat, message id) -> pending reject button.
        self.pending: dict[tuple[str, int], tuple[str, str]] = {}

    def attach(self, core: Any) -> None:
        self.core = core

    async def call(self, method: str, **params: Any) -> Any:
        """One Bot API call. Raises ``TelegramError`` with Telegram's description (never the token)."""
        try:
            response = await self.client.post(f"{self.base}/{method}", json=params, timeout=POLL_TIMEOUT + 15)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'failed')}")
        return body.get("result")

    # ---- outbound ------------------------------------------------------------------

    async def send(self, channel_id: str, message: Rendered, *, nonce: str | None = None) -> str | None:
        chat, topic = split_id(channel_id)
        params: dict[str, Any] = {
            "chat_id": chat,
            "text": message_html(message),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if topic is not None:
            params["message_thread_id"] = topic
        if message.reply_to_external and message.reply_to_external.isdigit():
            params["reply_parameters"] = {"message_id": int(message.reply_to_external), "allow_sending_without_reply": True}
        if message.buttons:
            params["reply_markup"] = {
                "inline_keyboard": [[{"text": b.label, "callback_data": b.custom_id} for b in message.buttons]]
            }
        result = await self.call("sendMessage", **params)
        return str(result["message_id"])

    async def create_thread(self, channel_id: str, name: str) -> str:
        chat, _ = split_id(channel_id)
        topic = await self.call("createForumTopic", chat_id=chat, name=name[:128])
        return f"{chat}:{topic['message_thread_id']}"

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "user": self.bot_username}

    async def _say(self, chat: str, topic: int | None, text: str, reply_to: int | None = None, **extra: Any) -> Any:
        params: dict[str, Any] = {"chat_id": chat, "text": telegram_html(text), "parse_mode": "HTML", **extra}
        if topic is not None:
            params["message_thread_id"] = topic
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            return await self.call("sendMessage", **params)
        except TelegramError as exc:
            logger.warning("could not answer in %s: %s", chat, exc)
            return None

    # ---- inbound -------------------------------------------------------------------------

    @staticmethod
    def where(message: dict[str, Any]) -> tuple[str, str | None, int | None]:
        chat = str(message["chat"]["id"])
        topic = message.get("message_thread_id") if message.get("is_topic_message") else None
        return (f"{chat}:{topic}" if topic else chat), (chat if topic else None), topic

    @staticmethod
    def name_of(user: dict[str, Any]) -> str:
        return user.get("username") or " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or str(user["id"])

    def command_of(self, text: str) -> CommandCall | None:
        first, _, rest = text.strip().partition(" ")
        bot = f"@{self.bot_username}".lower() if self.bot_username else None
        if bot and first.lower().endswith(bot):
            first = first[: -len(bot)]
        return parse_text_command(f"{first} {rest}")

    async def on_message(self, message: dict[str, Any]) -> str:
        user = message.get("from") or {}
        if not user or user.get("is_bot") or user.get("id") == self.bot_id:
            return "ignored"
        text = message.get("text") or message.get("caption") or ""
        channel_id, parent_id, topic = self.where(message)
        chat = str(message["chat"]["id"])
        user_id, user_name = str(user["id"]), self.name_of(user)
        prompt = (message.get("reply_to_message") or {}).get("message_id")
        pending = self.pending.pop((chat, prompt), None) if prompt is not None else None
        if pending is not None:
            custom_id, owner = pending
            if owner == user_id:
                reply = await self.core.button(ButtonPress(custom_id, channel_id, user_id, user_name, parent_id, text))
                await self._say(chat, topic, reply.text, message["message_id"])
                return "button"
            self.pending[(chat, prompt)] = pending  # someone else answered the prompt
        skeleton = self.command_of(text) if text.startswith(("/fleet", "!fleet")) else None
        if skeleton is not None:
            call = CommandCall(skeleton.name, skeleton.args, channel_id, user_id, user_name, parent_id)
            reply = await self.core.command(call)
            await self._say(chat, topic, reply.text, message["message_id"])
            return "command"
        result = await self.core.inbound(
            Inbound(
                channel_id=channel_id,
                message_id=str(message["message_id"]),
                user_id=user_id,
                user_name=user_name,
                text=text,
                parent_id=parent_id,
            )
        )
        config = self.core.config
        if result.status == "sent" and config is not None and config.settings.get("status_marks", True):
            try:
                await self.call(
                    "setMessageReaction",
                    chat_id=chat,
                    message_id=message["message_id"],
                    reaction=[{"type": "emoji", "emoji": "👌"}],
                )
            except TelegramError as exc:
                logger.debug("could not mark message: %s", exc)
        if result.status == "failed":
            await self._say(chat, topic, f"Not delivered to the fleet: {result.reason}", message["message_id"])
        elif result.note:
            await self._say(chat, topic, result.note, message["message_id"])
        return result.status

    async def on_callback(self, query: dict[str, Any]) -> Reply | None:
        custom_id = query.get("data")
        action = parse_button(custom_id)
        message = query.get("message") or {}
        if action is None or not message:
            await self._answer_callback(query["id"], "This button is not recognised.", alert=True)
            return None
        user = query["from"]
        user_id, user_name = str(user["id"]), self.name_of(user)
        channel_id, parent_id, topic = self.where(message)
        chat = str(message["chat"]["id"])
        if action.verb == "reject":
            if not self.core.config or not self.core.config.is_action_user(user_id):
                await self._answer_callback(query["id"], "You are not allowed to act on fleet cards.", alert=True)
                return Reply("You are not allowed to act on fleet cards.", ok=False)
            prompt = await self._say(
                chat, topic, REASON_PROMPT.format(ref=action.ref), message.get("message_id"),
                reply_markup={"force_reply": True, "selective": True},
            )
            if prompt is not None:
                if len(self.pending) >= PENDING_MAX:
                    self.pending.pop(next(iter(self.pending)))
                self.pending[(chat, prompt["message_id"])] = (custom_id, user_id)
            await self._answer_callback(query["id"], "Reply with a reason.")
            return None
        reply = await self.core.button(ButtonPress(custom_id, channel_id, user_id, user_name, parent_id))
        await self._answer_callback(query["id"], reply.text, alert=not reply.ok)
        return reply

    async def _answer_callback(self, query_id: str, text: str, *, alert: bool = False) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=query_id, text=text[:200], show_alert=alert)
        except TelegramError as exc:
            logger.debug("could not answer callback: %s", exc)

    async def poll_once(self, timeout: int = POLL_TIMEOUT) -> int:
        """One ``getUpdates`` round. Returns how many updates were handled."""
        updates = await self.call(
            "getUpdates", offset=self.offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )
        for update in updates or []:
            self.offset = max(self.offset, int(update["update_id"]) + 1)
            try:
                if "message" in update:
                    await self.on_message(update["message"])
                elif "callback_query" in update:
                    await self.on_callback(update["callback_query"])
            except Exception:  # noqa: BLE001 - one bad update must not stop polling
                logger.exception("telegram update %s failed", update.get("update_id"))
        return len(updates or [])


def run(core_factory: Any, bot_token: str) -> None:
    """Long-poll until interrupted."""
    if not re.fullmatch(r"[0-9]{5,12}:[A-Za-z0-9_-]{30,}", bot_token or ""):
        raise SystemExit("cat-fleet-relay --platform telegram needs TELEGRAM_BOT_TOKEN from @BotFather")

    async def main() -> None:
        async with httpx.AsyncClient() as client:
            adapter = TelegramAdapter(client, bot_token)
            core = core_factory(adapter)
            adapter.attach(core)
            me = await adapter.call("getMe")
            adapter.bot_id, adapter.bot_username = me["id"], me.get("username")
            await core.start()
            adapter.connected = True
            delay = 2.0
            try:
                while True:
                    try:
                        await adapter.poll_once()
                        delay = 2.0
                    except TelegramError as exc:
                        logger.warning("telegram poll failed: %s", exc)
                        adapter.connected = False
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 60.0)
                        adapter.connected = True
            finally:
                await core.stop()
                await core.hub.client.aclose()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
