"""Discord adapter for ``cat-fleet-relay``: messages, ``/fleet`` slash commands, buttons, threads, reactions.

Needs the optional extra ``pip install "cat-fleet-chat[discord]"`` and the
Message Content intent. Unlike ``cat-fleet-discord`` (inbound only, outbound
through a webhook), the bot posts route and reply deliveries itself, so cards
carry buttons, replies land in the thread they answer, and nothing needs a
webhook. ``discord`` is passed in rather than imported at module level, so
the logic here is testable without discord.py.

Buttons are handled in ``on_interaction`` by their ``cf1:`` id, not by a
registered view, so they keep working after a restart. Reaction actions
(``config.reactions`` and ``action_users``) reuse the legacy relay's
``ReactionActions`` with this relay's bindings.
"""

from __future__ import annotations

import logging
from typing import Any

from cat_fleet_chat.discord_relay import (
    MessageRefs,
    ReactionActions,
    message_text,
    status_reactions,
)
from cat_fleet_chat.relay.adapter import ButtonPress, CommandCall, Inbound, Reply
from cat_fleet_chat.relay.render import Rendered, parse_button
from cat_fleet_chat.relay.routing import MapEntry

logger = logging.getLogger("cat_fleet_chat.relay.discord")

FIELD_MAX = 25
STYLES = {"primary": "primary", "secondary": "secondary", "success": "success", "danger": "danger"}


def _snowflake(value: str) -> int | None:
    return int(value) if value.isdigit() else None


class DiscordAdapter:
    platform = "discord"

    def __init__(self, discord: Any, client: Any, refs: MessageRefs) -> None:
        self.discord = discord
        self.client = client
        self.refs = refs
        self.core: Any = None
        self.actions: ReactionActions | None = None
        self._actions_version: int | None = None

    def attach(self, core: Any) -> None:
        self.core = core

    # ---- outbound -------------------------------------------------------------

    async def _channel(self, channel_id: str) -> Any:
        ident = _snowflake(channel_id)
        if ident is None:
            raise ValueError(f"not a Discord channel id: {channel_id}")
        return self.client.get_channel(ident) or await self.client.fetch_channel(ident)

    def _embed(self, message: Rendered) -> Any:
        embed = self.discord.Embed(title=message.title, description=message.body, color=message.color)
        for name, value in message.fields[:FIELD_MAX]:
            embed.add_field(name=name[:256], value=value[:1024] or "—", inline=True)
        if message.footer:
            embed.set_footer(text=message.footer[:2048])
        return embed

    def _view(self, message: Rendered) -> Any:
        ui = self.discord.ui
        view = ui.View(timeout=None)
        for button in message.buttons:
            style = getattr(self.discord.ButtonStyle, STYLES.get(button.style, "secondary"))
            view.add_item(ui.Button(label=button.label, custom_id=button.custom_id, style=style))
        return view

    async def send(self, channel_id: str, message: Rendered, *, nonce: str | None = None) -> str | None:
        channel = await self._channel(channel_id)
        kwargs: dict[str, Any] = {
            "content": message.text[:2000] if message.title is None else None,
            # Agents cannot ping @everyone, roles or people through the relay.
            "allowed_mentions": self.discord.AllowedMentions.none(),
        }
        if message.title is not None:
            kwargs["embed"] = self._embed(message)
        if message.buttons:
            kwargs["view"] = self._view(message)
        if nonce:
            kwargs["nonce"] = nonce
        if message.reply_to_external and message.reply_to_external.isdigit():
            kwargs["reference"] = self.discord.MessageReference(
                message_id=int(message.reply_to_external), channel_id=int(channel_id), fail_if_not_exists=False
            )
        sent = await channel.send(**kwargs)
        return str(sent.id)

    async def create_thread(self, channel_id: str, name: str) -> str:
        channel = await self._channel(channel_id)
        thread = await channel.create_thread(
            name=name[:100], type=self.discord.ChannelType.public_thread, auto_archive_duration=1440
        )
        return str(thread.id)

    def status(self) -> dict[str, Any]:
        return {
            "connected": bool(self.client.is_ready()),
            "user": str(self.client.user) if self.client.user else None,
            "guilds": len(self.client.guilds),
        }

    # ---- inbound -----------------------------------------------------------------

    @staticmethod
    def where(channel: Any) -> tuple[str, str | None]:
        """``(channel_id, parent_id)``: a thread reports its parent channel."""
        parent = getattr(channel, "parent_id", None)
        return str(channel.id), (str(parent) if parent is not None else None)

    def inbound_of(self, message: Any) -> Inbound:
        channel_id, parent_id = self.where(message.channel)
        return Inbound(
            channel_id=channel_id,
            message_id=str(message.id),
            user_id=str(message.author.id),
            user_name=message.author.display_name,
            text=message.clean_content,
            parent_id=parent_id,
            is_bot=bool(message.author.bot or message.webhook_id is not None),
            attachments=tuple(attachment.url for attachment in message.attachments),
            raw=message,
        )

    def _sync_actions(self) -> ReactionActions | None:
        """Reaction actions over this relay's bindings, rebuilt when the config version moves."""
        config = self.core.config
        if config is None:
            return None
        if self.actions is None or self._actions_version != config.version:
            mapping: dict[int, MapEntry] = {}
            for binding in config.bindings:
                ident = _snowflake(binding["external_id"])
                if ident is not None and binding["kind"] == "channel":
                    mapping[ident] = config.entry(binding)
            users = frozenset(int(u) for u in config.settings.get("action_users", []) if str(u).isdigit())
            self.refs.ref_webhooks = frozenset(
                int(w) for w in config.settings.get("ref_webhooks", []) if str(w).isdigit()
            )
            self.actions = ReactionActions(
                mapping,
                self._forward,
                self.refs,
                users=users,
                reactions=config.settings.get("reactions"),
                react=self._react,
                prefix=config.prefix,
            )
            self._actions_version = config.version
        return self.actions

    async def _forward(self, payload: dict[str, Any]) -> tuple[bool, str]:
        """``ReactionActions`` posts through here: stamp this relay on the origin."""
        payload["origin"]["relay"] = self.core.name
        ok, body = await self.core.hub.post_message(payload)
        return ok, "" if ok else str(body)

    async def _react(self, post_id: int, kind: str, handle: str) -> tuple[bool, str]:
        ok, body = await self.core.hub.feed_react(post_id, kind, handle)
        return ok, "" if ok else str(body)

    async def on_message(self, message: Any) -> str:
        if self.core is None or (self.client.user is not None and message.author.id == self.client.user.id):
            return "ignored"
        inbound = self.inbound_of(message)
        result = await self.core.inbound(inbound)
        config = self.core.config
        if result.status == "sent" and result.hub_message_id is not None:
            self.refs.remember_text(
                int(message.id), message_text(message), hub_post_id=result.hub_message_id, hub_channel=result.hub_channel
            )
        if result.status != "ignored" and config is not None and config.settings.get("status_marks", True):
            users = frozenset(int(u) for u in config.settings.get("action_users", []) if str(u).isdigit())
            ok_mark, fail_mark = status_reactions(config.settings.get("reactions") or {}, users)
            try:
                await message.add_reaction(ok_mark if result.status == "sent" else fail_mark)
                if result.status == "failed":
                    await message.reply(f"Not delivered to the fleet: {result.reason}", mention_author=False)
                elif result.note:
                    await message.reply(result.note, mention_author=False)
            except Exception as exc:  # Discord permissions are the operator's
                logger.warning("could not mark message: %r", exc)
        actions = self._sync_actions()
        if actions is not None:
            await actions.observe(message)
        return result.status

    async def on_raw_reaction_add(self, payload: Any) -> str:
        actions = self._sync_actions() if self.core is not None else None
        if actions is None:
            return "ignored"
        return await actions.on_raw_reaction_add(payload, self.client)

    # ---- interactions -------------------------------------------------------------

    def _who(self, interaction: Any) -> tuple[str, str | None, str, str]:
        channel_id, parent_id = self.where(interaction.channel)
        user = interaction.user
        return channel_id, parent_id, str(user.id), getattr(user, "display_name", None) or user.name

    async def _answer(self, interaction: Any, reply: Reply, *, deferred: bool) -> None:
        none = self.discord.AllowedMentions.none()
        if deferred:
            await interaction.followup.send(reply.text[:2000] if reply.private else "Done.", ephemeral=True, allowed_mentions=none)
            if not reply.private:
                await interaction.channel.send(content=reply.text[:2000], allowed_mentions=none)
        else:
            await interaction.response.send_message(reply.text[:2000], ephemeral=reply.private, allowed_mentions=none)

    async def run_command(self, interaction: Any, name: str, args: dict[str, Any]) -> Reply:
        """A ``/fleet`` slash command. Deferred first: thread creation can take a while."""
        channel_id, parent_id, user_id, user_name = self._who(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        reply = await self.core.command(CommandCall(name, args, channel_id, user_id, user_name, parent_id))
        await self._answer(interaction, reply, deferred=True)
        return reply

    async def on_interaction(self, interaction: Any) -> Reply | None:
        """Card buttons (``cf1:`` ids) and the reject-reason modal. Other interactions are left alone."""
        data = getattr(interaction, "data", None) or {}
        custom_id = data.get("custom_id")
        action = parse_button(custom_id)
        if action is None or self.core is None:
            return None
        channel_id, parent_id, user_id, user_name = self._who(interaction)
        if action.verb == "reject" and data.get("components") is None:
            # First click: ask for the reason; the modal submit comes back here with components.
            if not self.core.config or not self.core.config.is_action_user(user_id):
                reply = Reply("You are not allowed to act on fleet cards.", ok=False)
                await self._answer(interaction, reply, deferred=False)
                return reply
            await interaction.response.send_modal(self.reject_modal(custom_id))
            return None
        reason = None
        for row in data.get("components") or []:
            for item in row.get("components", []):
                if item.get("custom_id") == "reason":
                    reason = item.get("value")
        press = ButtonPress(custom_id, channel_id, user_id, user_name, parent_id, reason)
        reply = await self.core.button(press)
        await self._answer(interaction, reply, deferred=False)
        return reply

    def reject_modal(self, custom_id: str) -> Any:
        ui = self.discord.ui
        modal = ui.Modal(title="Reject proposal", custom_id=custom_id, timeout=None)
        modal.add_item(
            ui.TextInput(label="Reason", custom_id="reason", style=self.discord.TextStyle.paragraph, max_length=500)
        )
        return modal


# ---- process wiring -------------------------------------------------------------


def register_commands(discord: Any, tree: Any, adapter: DiscordAdapter) -> Any:
    """The ``/fleet`` command group on ``tree``. Each subcommand hands off to ``RelayCore``."""
    app_commands = discord.app_commands
    group = app_commands.Group(name="fleet", description="Cat Fleet Chat")

    def command(name: str, description: str):
        return group.command(name=name, description=description)

    @command("help", "What the relay can do here")
    async def help_(interaction):
        await adapter.run_command(interaction, "help", {})

    @command("status", "Relay, hub and fleet status")
    async def status(interaction):
        await adapter.run_command(interaction, "status", {})

    @command("tasks", "Open tasks for this channel or another")
    async def tasks(interaction, channel: str | None = None, status: str | None = None):
        await adapter.run_command(interaction, "tasks", {"channel": channel, "status": status})

    @command("proposals", "Proposals (default: open)")
    async def proposals(interaction, status: str | None = None):
        await adapter.run_command(interaction, "proposals", {"status": status})

    @command("routes", "Event routes delivered to this channel")
    async def routes(interaction):
        await adapter.run_command(interaction, "routes", {})

    @command("post", "Post into a hub channel")
    async def post(interaction, channel: str, text: str):
        await adapter.run_command(interaction, "post", {"channel": channel, "text": text})

    @command("bind", "Bind this channel or thread to a hub channel (operators)")
    async def bind(interaction, channel: str, allowed: str | None = None):
        await adapter.run_command(interaction, "bind", {"channel": channel, "allowed": allowed})

    @command("unbind", "Remove this channel's binding (operators)")
    async def unbind(interaction):
        await adapter.run_command(interaction, "unbind", {})

    @command("subscribe", "Route a topic pattern or event kind here (operators)")
    async def subscribe(interaction, what: str, format: str = "line"):
        await adapter.run_command(interaction, "subscribe", {"what": what, "format": format})

    @command("unsubscribe", "Stop a route here (operators)")
    async def unsubscribe(interaction, route: int):
        await adapter.run_command(interaction, "unsubscribe", {"route": str(route)})

    @command("thread", "Open a thread bound to task:<id> or channel:<name> (operators)")
    async def thread(interaction, target: str):
        await adapter.run_command(interaction, "thread", {"target": target})

    tree.add_command(group)
    return group


def run(core_factory: Any, discord_token: str, refs: MessageRefs) -> None:
    """Log in and run until interrupted. ``core_factory(adapter)`` builds the ``RelayCore``."""
    try:
        import discord
    except ImportError:
        raise SystemExit("cat-fleet-relay --platform discord needs discord.py: pip install 'cat-fleet-chat[discord]'") from None

    intents = discord.Intents.default()
    intents.message_content = True
    intents.reactions = True

    class Relay(discord.Client):
        async def setup_hook(self) -> None:
            self.adapter = DiscordAdapter(discord, self, refs)
            self.core = await core_factory(self.adapter)
            self.adapter.attach(self.core)
            self.tree = discord.app_commands.CommandTree(self)
            register_commands(discord, self.tree, self.adapter)
            await self.core.start()
            guilds = [g for g in self.core.config.settings.get("guilds", []) if str(g).isdigit()]
            try:
                if guilds:
                    for guild in guilds:
                        target = discord.Object(id=int(guild))
                        self.tree.copy_global_to(guild=target)
                        await self.tree.sync(guild=target)
                else:
                    await self.tree.sync()
            except Exception as exc:  # a missing applications.commands scope must not stop relaying
                logger.warning("could not sync slash commands: %r", exc)

        async def on_ready(self) -> None:
            logger.info("cat-fleet-relay: logged in as %s", self.user)

        async def on_message(self, message) -> None:
            await self.adapter.on_message(message)

        async def on_raw_reaction_add(self, payload) -> None:
            await self.adapter.on_raw_reaction_add(payload)

        async def on_interaction(self, interaction) -> None:
            if interaction.type == discord.InteractionType.component or interaction.type == discord.InteractionType.modal_submit:
                await self.adapter.on_interaction(interaction)

        async def close(self) -> None:
            core = getattr(self, "core", None)
            if core is not None:
                await core.stop()
                await core.hub.client.aclose()
            await super().close()
            await refs.flush()

    try:
        Relay(intents=intents).run(discord_token, log_handler=None)
    except KeyboardInterrupt:
        pass
