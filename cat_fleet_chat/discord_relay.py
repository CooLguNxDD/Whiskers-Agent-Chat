"""``cat-fleet-discord``: relay Discord channel messages into Cat Fleet Chat.

Discord cannot call a webhook when someone types, so inbound from Discord needs
a bot. This process is that bot and a plain client of the hub, like
``cat-fleet-listen``: it logs in to Discord, and POSTs each message from a
mapped channel to ``/api/v1/messages``. It needs no public URL.

The other direction (hub to Discord) is an outbound webhook of format
``discord``; give it ``exclude_authors=["dc-*"]`` so relayed messages are not
echoed back to the channel they came from.

Each relayed message carries an ``origin`` (the Discord channel and message),
so when an agent answers with ``reply_to``, the hub sends the answer through
the outbound webhook attached to that Discord channel.

Typing ``#ops`` in a message posts it to hub channel ``ops`` instead of the
mapped default, if ``--map`` allows it.

Needs the optional extra: ``pip install "cat-fleet-chat[discord]"``, and the
Message Content intent enabled for the bot in the Discord developer portal.

Examples::

    DISCORD_BOT_TOKEN=... cat-fleet-discord --map 123456789012345678=fleet
    DISCORD_BOT_TOKEN=... cat-fleet-discord --map 123456789012345678=fleet,ops
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from cat_fleet_chat.evolve import parse_command
from cat_fleet_chat.validate import CHANNEL_RE, HANDLE_RE

DEFAULT_URL = "http://127.0.0.1:8787"
DEFAULT_PREFIX = "dc-"
OK_REACTION = "✅"
FAIL_REACTION = "❌"
# Delivery marks used instead when ✅/❌ are reaction actions, so a delivered
# message never reads as both approved and rejected. Reserved: never actions.
ALT_OK_REACTION = "📨"
ALT_FAIL_REACTION = "⚠️"
ATTEMPTS = 3
RETRY_DELAY = 2.0

Forward = Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class MapEntry:
    """Where one Discord channel may post in the hub.

    ``default`` takes every message with no ``#channel`` token. A token may
    only pick a channel in ``allowed``, or any channel when ``any_channel`` is
    set, so a Discord channel cannot reach hub channels you did not list.
    """

    default: str
    allowed: frozenset[str]
    any_channel: bool = False


def parse_map(items: list[str]) -> dict[int, MapEntry]:
    """``["123=fleet,ops", "456=*"]`` into per-channel entries. The first name is the default.

    ``*`` lets a ``#token`` pick any hub channel. Rejects malformed entries.
    """
    mapping: dict[int, MapEntry] = {}
    for item in items:
        left, sep, right = item.partition("=")
        names = [part.strip() for part in right.split(",") if part.strip()]
        valid = sep and left.strip().isdigit() and names
        if not valid or not CHANNEL_RE.fullmatch(names[0]):
            raise ValueError(
                f"--map expects <discord_channel_id>=<hub_channel>[,<more channels>|*], got {item!r}"
            )
        any_channel = "*" in names[1:]
        extras = [name for name in names[1:] if name != "*"]
        if not all(CHANNEL_RE.fullmatch(name) for name in extras):
            raise ValueError(f"--map has an invalid hub channel name in {item!r}")
        mapping[int(left.strip())] = MapEntry(names[0], frozenset([names[0], *extras]), any_channel)
    if not mapping:
        raise ValueError("at least one --map is required")
    return mapping


# A hub channel written as #name, standing alone: not part of a word, a URL
# fragment or another token.
TARGET_RE = re.compile(r"(?<![\w#/])#([a-z0-9][a-z0-9_-]{0,63})(?![\w-])")


def parse_target(
    text: str, entry: MapEntry, known: frozenset[str] | set[str] | None
) -> tuple[str, str, str | None]:
    """Pick the hub channel for ``text``. Returns ``(channel, text, note)``.

    The first ``#name`` that is a hub channel becomes the target and is removed
    from the text. A ``#name`` the hub does not have is left alone (it may be a
    Discord channel). A hub channel this Discord channel may not reach keeps
    the default and returns a note. With no channel list (``known`` is None)
    nothing is targeted.
    """
    if known is None:
        return entry.default, text, None
    for match in TARGET_RE.finditer(text):
        name = match.group(1)
        if name not in known:
            continue
        if not (entry.any_channel or name in entry.allowed):
            return entry.default, text, f"#{name} is not allowed from this Discord channel"
        cleaned = re.sub(r"[ \t]{2,}", " ", text[: match.start()] + text[match.end() :]).strip()
        return name, cleaned or text, None
    return entry.default, text, None


def author_handle(display_name: str, prefix: str = DEFAULT_PREFIX) -> str:
    """A hub handle for a Discord user. Always starts with ``prefix`` so hooks can filter on it."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_")
    return (prefix + (slug or "user"))[:64].rstrip("-_")


def message_text(message: Any) -> str:
    """Message body plus attachment links. ``clean_content`` resolves ``<@id>`` to names."""
    parts = [message.clean_content.strip()] if message.clean_content.strip() else []
    parts.extend(attachment.url for attachment in message.attachments)
    return "\n".join(parts)


def build_post(
    message: Any,
    hub_channel: str,
    prefix: str = DEFAULT_PREFIX,
    *,
    text: str | None = None,
    origin_channel_id: int | None = None,
) -> dict[str, Any]:
    """The hub post for a Discord message, with the ``origin`` that lets a reply find its way back."""
    return {
        "channel": hub_channel,
        "author": author_handle(message.author.display_name, prefix),
        "text": message_text(message) if text is None else text,
        # Idempotent: a retry after a timeout cannot post the message twice.
        "client_request_id": f"discord-{message.id}",
        "origin": {
            "source": "discord",
            # The mapped channel, not a thread inside it: an attached webhook posts to the channel.
            "channel_id": str(origin_channel_id if origin_channel_id is not None else message.channel.id),
            "message_id": str(message.id),
            "author": message.author.display_name[:80],
        },
    }


def make_forward(
    client: httpx.AsyncClient, url: str, token: str, *, refs: MessageRefs | None = None
) -> Forward:
    """POST a payload to the hub. Retries network errors and 5xx; the request id makes that safe."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    endpoint = f"{url.rstrip('/')}/api/v1/messages"

    async def forward(payload: dict[str, Any]) -> tuple[bool, str]:
        reason = "unknown error"
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY * attempt)
            try:
                response = await client.post(endpoint, json=payload, headers=headers, timeout=15)
            except httpx.HTTPError as exc:
                reason = f"hub unreachable ({type(exc).__name__})"
                continue
            if response.status_code == 201:
                # Only original relays establish a chat reference. Reaction-command
                # replies must never overwrite the reference of their target.
                if refs is not None and re.fullmatch(r"discord-[0-9]+", payload.get("client_request_id", "")):
                    try:
                        post_id = response.json()["message"]["id"]
                        refs.remember_text(
                            int(payload["origin"]["message_id"]), payload["text"],
                            hub_post_id=post_id, hub_channel=payload["channel"],
                        )
                    except (ValueError, KeyError, TypeError):
                        pass  # delivery still succeeded, even with no usable reference
                return True, ""
            try:
                reason = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                reason = f"HTTP {response.status_code}"
            if response.status_code < 500:
                break
        return False, reason

    return forward


ChannelList = Callable[[], Awaitable["set[str] | None"]]


def make_channels(
    client: httpx.AsyncClient, url: str, token: str, ttl: float = 30.0
) -> ChannelList:
    """The hub's open channel names, cached for ``ttl`` seconds. ``None`` when the hub was never reachable."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    endpoint = f"{url.rstrip('/')}/api/v1/channels"
    cache: dict[str, Any] = {"at": 0.0, "names": None}

    async def channels() -> set[str] | None:
        now = time.monotonic()
        if cache["names"] is not None and now - cache["at"] < ttl:
            return cache["names"]
        try:
            response = await client.get(endpoint, headers=headers, timeout=10)
            response.raise_for_status()
            names = {item["name"] for item in response.json()["channels"]}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return cache["names"]  # a stale list beats none
        cache.update(at=now, names=names)
        return names

    return channels


async def handle_message(
    message: Any,
    mapping: dict[int, MapEntry],
    forward: Forward,
    prefix: str = DEFAULT_PREFIX,
    channels: ChannelList | None = None,
    *,
    ok_reaction: str = OK_REACTION,
    fail_reaction: str = FAIL_REACTION,
) -> str:
    """Relay one Discord message. Returns ``ignored``, ``sent`` or ``failed``.

    Bots and webhook posts are ignored, which stops the hub's own outbound
    webhook (a Discord webhook post) from looping back in. A ``#hub-channel``
    token picks the hub channel, within what the map allows. The delivery
    marks come from ``status_reactions`` when reaction actions are on.
    """
    if message.author.bot or message.webhook_id is not None:
        return "ignored"
    channel = message.channel
    mapped_id = channel.id if channel.id in mapping else getattr(channel, "parent_id", None)  # or a thread of one
    entry = mapping.get(mapped_id)
    if entry is None:
        return "ignored"
    text = message_text(message)
    if not text:
        return "ignored"  # sticker-only or similar
    known = await channels() if channels is not None and TARGET_RE.search(text) else None
    hub_channel, text, note = parse_target(text, entry, known)
    payload = build_post(message, hub_channel, prefix, text=text, origin_channel_id=mapped_id)
    ok, reason = await forward(payload)
    try:
        if ok:
            await message.add_reaction(ok_reaction)
            if note:
                await message.reply(f"Sent to #{hub_channel} instead: {note}", mention_author=False)
        else:
            await message.add_reaction(fail_reaction)
            await message.reply(f"Not delivered to the fleet: {reason}", mention_author=False)
    except Exception as exc:  # Discord permissions are the operator's; never crash the relay
        sys.stderr.write(f"cat-fleet-discord: could not react or reply: {exc!r}\n")
    return "sent" if ok else "failed"


# Reaction configuration belongs to the optional relay, not the hub Settings.
DEFAULT_REACTIONS = {
    "✅": "approve {P}", "❌": "reject {P} via Discord reaction",
    "⏸️": "pause evolve", "▶️": "resume evolve",
    **{chr(0x1F1E6 + n): "{step} answer: (" + chr(97 + n) + ")" for n in range(5)},
    "👍": "+1", "👎": "-1",
}
PROPOSAL_RE = re.compile(r"\bP[1-9][0-9]{0,17}\b", re.IGNORECASE)
STEP_RE = re.compile(r"\[(S[0-9]{2,}(?:-[A-Za-z0-9_-]+)?)\]")
OPTION_RE = re.compile(r"\(([a-e])\)", re.IGNORECASE)
ANSWER_RE = re.compile(r"\{step\} answer: \(([a-e])\)")
REF_PREFIX = "cat-fleet-ref:"
MAX_ACTION_REACTIONS = 12


def parse_reactions(raw: str | None) -> dict[str, str]:
    """Parse CAT_FLEET_DISCORD_REACTIONS; empty/unset uses the defaults.

    A nonempty value REPLACES the map: ``✅=approve {P};❌=reject {P} via
    Discord reaction``. Values are evolve commands with ``{P}``, pause/resume,
    ``{step} answer: (a)`` .. ``(e)``, or feed kinds ``+1`` / ``-1``.
    Literal proposal ids, unknown placeholders, duplicates and empty entries
    are errors. Unicode emoji keys may be remapped; at most 12 actions.
    ``📨`` and ``⚠️`` are reserved for delivery status (``status_reactions``).
    """
    if raw is None or not raw.strip():
        return dict(DEFAULT_REACTIONS)
    result: dict[str, str] = {}
    for item in raw.split(";"):
        emoji, sep, template = item.partition("=")
        emoji, template = emoji.strip(), template.strip()
        command = parse_command(template.replace("{P}", "P1"))
        valid_command = command is not None and (
            command.number is None or (
                re.match(r"[/!]?(approve|reject|edit)[ \t]+\{P\}(?:[ \t]|$)", template, re.IGNORECASE)
                and template.count("{P}") == 1 and "{step}" not in template
            )
        )
        valid = template in {"+1", "-1"} or ANSWER_RE.fullmatch(template) or valid_command
        # Only supported literal placeholders; str.format conversions and
        # attribute/index traversal are deliberately not part of this grammar.
        remainder = template.replace("{P}", "").replace("{step}", "")
        if (not sep or not emoji or len(emoji) > 16 or not any(ord(c) > 0x2000 for c in emoji)
                or any(c.isspace() for c in emoji)
                or _bare(emoji) in {_bare(ALT_OK_REACTION), _bare(ALT_FAIL_REACTION)}
                or any(_bare(key) == _bare(emoji) for key in result)
                or not valid or "{" in remainder or "}" in remainder
                or "\n" in template or "\r" in template):
            raise ValueError(f"CAT_FLEET_DISCORD_REACTIONS: invalid entry {item!r}")
        result[emoji] = template
    if len(result) > MAX_ACTION_REACTIONS:
        raise ValueError("CAT_FLEET_DISCORD_REACTIONS: at most 12 actions are allowed")
    return result


def _bare(emoji: str) -> str:
    """Discord may omit the optional variation selector (pause/play, warning)."""
    return emoji.replace("\ufe0f", "")


def status_reactions(reactions: dict[str, str], users: frozenset[int]) -> tuple[str, str]:
    """Delivery marks ``(ok, fail)`` for ``handle_message``.

    ✅/❌ as before, unless reaction actions are on (``users`` non-empty) and
    use that emoji: then 📨/⚠️, so a status mark is never also a command.
    """
    actions = {_bare(key) for key in reactions} if users else set()
    ok = ALT_OK_REACTION if _bare(OK_REACTION) in actions else OK_REACTION
    fail = ALT_FAIL_REACTION if _bare(FAIL_REACTION) in actions else FAIL_REACTION
    return ok, fail


def _parse_ids(name: str, raw: str | None) -> frozenset[int]:
    if raw is None or not raw.strip():
        return frozenset()
    parts = [part.strip() for part in raw.split(",")]
    if not all(re.fullmatch(r"[1-9][0-9]{0,19}", part) for part in parts):
        raise ValueError(f"{name} must be comma-separated Discord ids")
    return frozenset(int(part) for part in parts)


def parse_reaction_users(raw: str | None) -> frozenset[int]:
    """CAT_FLEET_DISCORD_REACTION_USERS: Discord user ids; nobody by default."""
    return _parse_ids("CAT_FLEET_DISCORD_REACTION_USERS", raw)


def parse_ref_webhooks(raw: str | None) -> frozenset[int]:
    """CAT_FLEET_DISCORD_REF_WEBHOOKS: ids of the hub's own Discord webhooks.

    The id is the number after ``/api/webhooks/`` in the hook URL. Only posts
    from these webhooks may set a target through a ``cat-fleet-ref:`` footer;
    empty means no footer is trusted.
    """
    return _parse_ids("CAT_FLEET_DISCORD_REF_WEBHOOKS", raw)


@dataclass(frozen=True)
class MessageRef:
    # Chat messages and feed posts have independent id spaces. Never use a
    # chat message id for a feed vote, or a feed id as chat reply_to.
    hub_post_id: int | None = None
    feed_post_id: int | None = None
    hub_channel: str | None = None
    proposal: str | None = None
    step: str | None = None
    options: tuple[str, ...] = ()
    evolve: bool = False


def _log(context: str, exc: object) -> None:
    sys.stderr.write(f"cat-fleet-discord: {context}: {exc!r}\n")


class MessageRefs:
    """Oldest-first bounded references, atomically persisted without credentials.

    One relay process owns a state file. Bad/unreadable state is logged and
    rebuilt from fetched Discord text; a state write failure never loses a
    delivered message or crashes the Discord event handler.
    """

    def __init__(
        self, path: str | Path, limit: int = 5000, *, ref_webhooks: frozenset[int] = frozenset(),
    ):
        if limit < 1:
            raise ValueError("message reference limit must be positive")
        self.path = Path(path)
        self.limit = limit
        # Webhooks whose ref footers are trusted (the hub's outbound hooks).
        self.ref_webhooks = ref_webhooks
        self._refs: dict[int, MessageRef] = {}
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("state must be a list")
            for key, data in rows[-limit:]:
                if not isinstance(data, dict):
                    raise ValueError("state reference must be an object")
                data["options"] = tuple(data.get("options", ()))
                ref = MessageRef(**data)
                if (type(ref.evolve) is not bool
                        or any(option not in ("a", "b", "c", "d", "e") for option in ref.options)
                        or (ref.proposal is not None and not PROPOSAL_RE.fullmatch(ref.proposal))
                        or (ref.step is not None and not STEP_RE.fullmatch(f"[{ref.step}]"))
                        or (ref.hub_channel is not None and not CHANNEL_RE.fullmatch(ref.hub_channel))):
                    raise ValueError("invalid extracted reference in state")
                if not isinstance(key, int) or key <= 0:
                    raise ValueError("invalid Discord message id in state")
                for post_id in (ref.hub_post_id, ref.feed_post_id):
                    if post_id is not None and (type(post_id) is not int or post_id <= 0):
                        raise ValueError("invalid hub reference in state")
                self._refs[key] = ref
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            self._refs.clear()
            _log("could not load reaction state", exc)

    def __len__(self) -> int:
        return len(self._refs)

    def get(self, message_id: int) -> MessageRef | None:
        return self._refs.get(message_id)

    def remember_text(
        self, message_id: int, text: str, *, hub_post_id: int | None = None,
        feed_post_id: int | None = None, hub_channel: str | None = None,
    ) -> MessageRef:
        prior = self.get(message_id) or MessageRef()
        proposals = set(p.upper() for p in PROPOSAL_RE.findall(text))
        steps = set(STEP_RE.findall(text))
        ref = MessageRef(
            hub_post_id=hub_post_id if hub_post_id is not None else prior.hub_post_id,
            feed_post_id=feed_post_id if feed_post_id is not None else prior.feed_post_id,
            hub_channel=hub_channel if hub_channel is not None else prior.hub_channel,
            proposal=next(iter(proposals)) if len(proposals) == 1 else None,
            step=next(iter(steps)) if len(steps) == 1 else None,
            options=tuple(sorted(set(o.lower() for o in OPTION_RE.findall(text)))),
            evolve=bool(re.search(r"\bevolve\b", text, re.IGNORECASE)),
        )
        if self.get(message_id) != ref:
            self._refs[message_id] = ref
            while len(self._refs) > self.limit:
                del self._refs[next(iter(self._refs))]
            self._changed()
        return ref

    def remember(self, message: Any, **known: Any) -> MessageRef:
        # Single-entity webhook payloads carry typed metadata. Coalesced posts
        # intentionally have no reference: choosing one would target the wrong id.
        # Only the hub's own webhooks are trusted; another integration in a
        # mapped channel must not aim a human's vote or reply at any hub id.
        if getattr(message, "webhook_id", None) in self.ref_webhooks:
            for embed in getattr(message, "embeds", []):
                footer = getattr(getattr(embed, "footer", None), "text", None)
                if not isinstance(footer, str) or not footer.startswith(REF_PREFIX):
                    continue
                try:
                    data = json.loads(footer[len(REF_PREFIX):])
                    post_id = data["id"]
                    if type(post_id) is not int or post_id <= 0:
                        continue
                    if data["kind"] == "feed":
                        known.setdefault("feed_post_id", post_id)
                    elif data["kind"] == "message" and CHANNEL_RE.fullmatch(data.get("channel", "")):
                        known.setdefault("hub_post_id", post_id)
                        known.setdefault("hub_channel", data["channel"])
                except (ValueError, KeyError, TypeError, RecursionError):
                    continue
        return self.remember_text(message.id, message_text(message), **known)

    def _changed(self) -> None:
        """Persist a change. Inside the event loop the write runs in a thread,
        one at a time and coalesced, so disk I/O never blocks the Discord client."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write(self._snapshot())
            return
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while self._dirty:
            self._dirty = False
            await asyncio.to_thread(self._write, self._snapshot())

    async def flush(self) -> None:
        """Wait for pending background writes (shutdown, tests)."""
        while self._flush_task is not None and not self._flush_task.done():
            await self._flush_task

    def _snapshot(self) -> str:
        # Serialised on the loop: the writer thread never iterates the live dict.
        return json.dumps([[key, asdict(ref)] for key, ref in self._refs.items()])

    def _write(self, data: str) -> None:
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            _log("could not save reaction state", exc)
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError as exc:
                    _log("could not clean temporary reaction state", exc)


FeedReact = Callable[[int, str, str], Awaitable[tuple[bool, str]]]


def make_feed_react(client: httpx.AsyncClient, url: str, token: str) -> FeedReact:
    """Idempotent PUT for a human's feed vote, using the same retry policy as messages."""
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async def react(post_id: int, kind: str, handle: str) -> tuple[bool, str]:
        endpoint = f"{url.rstrip('/')}/api/v1/feed/posts/{post_id}/reactions/{kind}"
        reason = "unknown error"
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_DELAY * attempt)
            try:
                response = await client.put(endpoint, json={"handle": handle}, headers=headers, timeout=15)
            except httpx.HTTPError as exc:
                reason = f"hub unreachable ({type(exc).__name__})"
                continue
            if response.is_success:
                return True, ""
            try:
                reason = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                reason = f"HTTP {response.status_code}"
            if response.status_code < 500:
                break
        return False, reason

    return react


class ReactionActions:
    """Testable Discord event logic. The hub remains the command authority."""

    def __init__(
        self, mapping: dict[int, MapEntry], forward: Forward, refs: MessageRefs, *,
        users: frozenset[int] = frozenset(), reactions: dict[str, str] | None = None,
        react: FeedReact | None = None, prefix: str = DEFAULT_PREFIX, pace: float = 0.25,
    ):
        self.mapping, self.forward, self.refs = mapping, forward, refs
        self.users = users
        self.reactions = dict(DEFAULT_REACTIONS) if reactions is None else reactions
        self.react, self.prefix, self.pace = react, prefix, pace
        self._add_lock = asyncio.Lock()

    def mapped_id(self, channel: Any) -> int | None:
        return channel.id if channel.id in self.mapping else getattr(channel, "parent_id", None)

    @staticmethod
    def command(template: str, ref: MessageRef) -> str | None:
        if template in {"+1", "-1"}:
            return template if ref.feed_post_id is not None else None
        if "{P}" in template:
            return template.replace("{P}", ref.proposal) if ref.proposal else None
        if match := ANSWER_RE.fullmatch(template):
            return template.replace("{step}", ref.step) if ref.step and match[1] in ref.options else None
        return template if ref.evolve else None

    async def observe(self, message: Any) -> None:
        """Pre-add applicable actions, including on webhook posts, never their text."""
        if not self.users or self.mapped_id(message.channel) not in self.mapping:
            return
        ref = self.refs.remember(message)
        emojis = [emoji for emoji, template in self.reactions.items() if self.command(template, ref)]
        # Globally serialised and capped per message; discord.py handles rate
        # limits, and a failed add stops the batch rather than hammering Discord.
        async with self._add_lock:
            for emoji in emojis[:MAX_ACTION_REACTIONS]:
                try:
                    await asyncio.sleep(self.pace)
                    await message.add_reaction(emoji)
                except Exception as exc:
                    _log("could not pre-add reaction", exc)
                    break

    async def on_raw_reaction_add(self, payload: Any, client: Any) -> str:
        if payload.user_id not in self.users or payload.user_id == getattr(client.user, "id", None):
            return "ignored"
        emoji = str(payload.emoji)
        template = self.reactions.get(emoji)
        if template is None:
            template = next((value for key, value in self.reactions.items()
                             if _bare(key) == _bare(emoji)), None)
        if template is None:
            return "ignored"
        try:
            channel = client.get_channel(payload.channel_id)
            if channel is None:
                channel = await client.fetch_channel(payload.channel_id)
            mapped_id = self.mapped_id(channel)
            if mapped_id not in self.mapping:
                return "ignored"
            message = await channel.fetch_message(payload.message_id)
            human = getattr(payload, "member", None) or await client.fetch_user(payload.user_id)
            if human.bot:
                return "ignored"
        except Exception as exc:
            _log("could not fetch reaction target or human", exc)
            return "ignored"
        ref = self.refs.remember(message)
        command = self.command(template, ref)
        if command is None:
            return "ignored"
        handle = author_handle(human.display_name, self.prefix)
        if command in {"+1", "-1"}:
            if self.react is None:
                return "ignored"
            ok, reason = await self.react(ref.feed_post_id, command, handle)
        else:
            entry = self.mapping[mapped_id]
            hub_channel = ref.hub_channel or entry.default
            if not (entry.any_channel or hub_channel in entry.allowed):
                return "ignored"  # a changed --map must not be bypassed by saved state
            post = build_post(message, hub_channel, self.prefix, text=command, origin_channel_id=mapped_id)
            post["author"] = handle
            post["origin"]["author"] = human.display_name[:80]
            codepoints = "-".join(f"{ord(c):x}" for c in emoji)
            post["client_request_id"] = f"discord-react-{message.id}-{payload.user_id}-{codepoints}"
            if ref.hub_post_id is not None:
                post["reply_to"] = ref.hub_post_id
            ok, reason = await self.forward(post)
        if not ok:
            _log("reaction action not delivered", reason)
        return "sent" if ok else "failed"

    async def on_raw_reaction_remove(self, payload: Any, client: Any) -> None:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cat-fleet-discord",
        description="Relay Discord channel messages into Cat Fleet Chat channels.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CAT_FLEET_URL", DEFAULT_URL),
        help="hub base URL (env CAT_FLEET_URL, default %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CAT_FLEET_TOKEN", ""),
        help="hub bearer token (env CAT_FLEET_TOKEN)",
    )
    parser.add_argument(
        "--discord-token",
        default=os.environ.get("DISCORD_BOT_TOKEN", ""),
        help="Discord bot token (env DISCORD_BOT_TOKEN)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="DISCORD_CHANNEL_ID=HUB_CHANNEL[,MORE|*]",
        help=(
            "relay this Discord channel into a hub channel (repeatable). The first name is the "
            "default; extra names are channels a #token may pick; * allows any"
        ),
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="prefix for relayed authors, so outbound hooks can exclude them (default %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        mapping = parse_map(args.map)
        reactions = parse_reactions(os.environ.get("CAT_FLEET_DISCORD_REACTIONS"))
        reaction_users = parse_reaction_users(os.environ.get("CAT_FLEET_DISCORD_REACTION_USERS"))
        ref_webhooks = parse_ref_webhooks(os.environ.get("CAT_FLEET_DISCORD_REF_WEBHOOKS"))
    except ValueError as exc:
        parser.error(str(exc))
    if not args.discord_token:
        parser.error("a Discord bot token is required (--discord-token or DISCORD_BOT_TOKEN)")
    if not HANDLE_RE.fullmatch(args.prefix + "x"):
        parser.error("--prefix must be letters, digits, '_' or '-', starting with a letter or digit")
    try:
        import discord
    except ImportError:
        raise SystemExit(
            "cat-fleet-discord needs discord.py: pip install 'cat-fleet-chat[discord]'"
        ) from None

    intents = discord.Intents.default()
    intents.message_content = True
    intents.reactions = True
    refs = MessageRefs(
        os.environ.get("CAT_FLEET_DISCORD_STATE", "cat_fleet_discord_state.json"),
        ref_webhooks=ref_webhooks,
    )
    ok_reaction, fail_reaction = status_reactions(reactions, reaction_users)

    class Relay(discord.Client):
        async def setup_hook(self) -> None:
            self.http_client = httpx.AsyncClient()
            self.forward = make_forward(self.http_client, args.url, args.token, refs=refs)
            self.channels = make_channels(self.http_client, args.url, args.token)
            self.actions = ReactionActions(
                mapping, self.forward, refs, users=reaction_users, reactions=reactions,
                react=make_feed_react(self.http_client, args.url, args.token), prefix=args.prefix,
            )

        async def on_ready(self) -> None:
            sys.stderr.write(
                f"cat-fleet-discord: logged in as {self.user}; relaying {len(mapping)} channel(s)\n"
            )

        async def on_message(self, message: Any) -> None:
            # Establish the chat ref before exposing actions to a fast-clicking
            # human. Webhook/bot text is ignored by handle_message, but observed.
            await handle_message(
                message, mapping, self.forward, args.prefix, self.channels,
                ok_reaction=ok_reaction, fail_reaction=fail_reaction,
            )
            await self.actions.observe(message)

        async def on_raw_reaction_add(self, payload: Any) -> None:
            await self.actions.on_raw_reaction_add(payload, self)

        async def on_raw_reaction_remove(self, payload: Any) -> None:
            pass  # Removing an emoji never undoes a command or a hub vote.

        async def close(self) -> None:
            await super().close()
            await self.http_client.aclose()
            await refs.flush()

    try:
        Relay(intents=intents).run(args.discord_token)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
