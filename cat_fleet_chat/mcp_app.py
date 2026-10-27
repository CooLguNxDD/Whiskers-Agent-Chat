"""Standalone FastMCP tools. Same operations as the REST API, different envelope on errors."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

from cat_fleet_chat.errors import INTERNAL_ERROR, HubError
from cat_fleet_chat.hub import Hub

logger = logging.getLogger("cat_fleet_chat.mcp_app")

_hub: Hub | None = None
_dispatcher: Any = None  # WebhookDispatcher | None; only test_webhook needs it


def set_hub(hub: Hub | None) -> None:
    global _hub
    _hub = hub


def set_dispatcher(dispatcher: Any) -> None:
    global _dispatcher
    _dispatcher = dispatcher


def get_hub() -> Hub:
    if _hub is None:
        raise HubError("unavailable", "hub is not running", 503)
    return _hub


async def post_message(
    channel: str,
    author: str,
    text: str,
    reply_to: int | None = None,
    client_request_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    destination: str | None = None,
) -> dict[str, Any]:
    """Post a message into a channel.

    Mentions are @handles at token boundaries (not email addresses) and are
    stored at write time. ``client_request_id`` makes a retry return the
    original message instead of posting a second one. A different payload
    with the same key is a conflict.

    To also send the message to a Discord channel, pass ``destination``: the
    name of an outbound webhook from ``list_destinations``. To answer a message
    that came from Discord (it carries ``origin``), pass ``reply_to=<its id>``
    and the reply is sent back to the Discord channel it came from. If the
    result has ``warnings``, nothing was sent to Discord.

    ``attachments`` are descriptors of objects already uploaded to the object
    store (``filename``, ``content_type``, ``size_bytes``, ``storage="minio"``,
    ``bucket``, ``object_key``, optional ``sha256``). The hub records them and
    never fetches the bytes. Archived channels reject posts.
    """
    try:
        return await get_hub().post_message(
            channel,
            author,
            text,
            reply_to=reply_to,
            client_request_id_value=client_request_id,
            attachments_value=attachments,
            destination_value=destination,
        )
    except HubError as exc:
        return exc.mcp_body()


async def list_destinations() -> dict[str, Any]:
    """Discord channels (outbound webhooks) you can send to with ``post_message(destination=...)``.

    Each has a ``name``, a ``description`` of what it is for, and its
    ``discord_channel_id`` when known. ``directed_only`` means it receives only
    messages addressed to it.
    """
    try:
        return await get_hub().list_destinations()
    except HubError as exc:
        return exc.mcp_body()


async def get_messages(
    channel: str,
    since_id: int | None = None,
    before_id: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read messages. ``since_id`` pages forward, ``before_id`` pages backward.

    Do not send both cursors. The result is ascending by id and includes
    ``has_more``, a message ``cursor``, and the SSE ``event_cursor`` from the
    same read.
    """
    try:
        return await get_hub().get_messages(
            channel, since_id=since_id, before_id=before_id, limit=limit
        )
    except HubError as exc:
        return exc.mcp_body()


async def wait_for_mentions(
    agent: str,
    since_id: int | None = 0,
    channel: str | None = None,
    timeout: int | None = None,
    limit: int | None = None,
    mail_since_id: int | None = None,
) -> dict[str, Any]:
    """Park until ``agent`` is mentioned in a message with id greater than ``since_id``, or gets mail.

    ``timeout`` is clamped to 1..300 seconds (default 60). An empty result
    keeps the cursor you passed and sets ``timed_out`` true. Drain ``has_more``
    pages before waiting again. Pass a shorter timeout when the MCP client
    cuts tools off sooner than the hub would return.

    Direct mail also wakes the wait: ``mail`` lists mail after
    ``mail_since_id`` and ``mail_unread`` counts unread mail. Send back
    ``mail_cursor`` as ``mail_since_id``; without it only mail that arrives
    during this call wakes you.
    """
    try:
        return await get_hub().wait_for_mentions(
            agent,
            since_id,
            channel=channel,
            timeout=timeout,
            limit=limit,
            mail_since_id=mail_since_id,
        )
    except HubError as exc:
        return exc.mcp_body()


async def fleet_mail_send(
    sender: str,
    recipient: str,
    body: str,
    subject: str | None = None,
    reply_to: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Send a direct mail to one handle's private inbox. It wakes that agent's wait.

    ``subject`` is optional (one line, up to 200 characters). ``reply_to`` is
    the id of a mail you sent or received. ``client_request_id`` makes a retry
    return the original mail. Use channels for anything the fleet should see.
    """
    try:
        return await get_hub().send_mail(
            sender,
            recipient,
            body,
            subject=subject,
            reply_to=reply_to,
            client_request_id=client_request_id,
        )
    except HubError as exc:
        return exc.mcp_body()


async def fleet_mail_list(
    agent: str,
    state: str = "unread",
    since_id: int | None = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """List ``agent``'s mail, oldest first. ``state`` is unread (default), read or all.

    Page forward by sending the returned ``cursor`` as ``since_id`` while
    ``has_more`` is true. Listing does not mark anything read.
    """
    try:
        result = await get_hub().list_mail(agent, state=state, since_id=since_id, limit=limit)
        result.update(await get_hub().mail_counts(agent))
        return result
    except HubError as exc:
        return exc.mcp_body()


async def fleet_mail_read(
    agent: str,
    ids: list[int] | None = None,
    up_to_id: int | None = None,
) -> dict[str, Any]:
    """Mark your mail read: the given ``ids``, or everything up to ``up_to_id``. Safe to repeat."""
    try:
        return await get_hub().mark_mail_read(agent, ids=ids, up_to_id=up_to_id)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_mail_ack(agent: str, ids: list[int]) -> dict[str, Any]:
    """Acknowledge your mail (you acted on it). Ack implies read. Safe to repeat."""
    try:
        return await get_hub().ack_mail(agent, ids=ids)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_publish(
    publisher: str,
    topic: str,
    data: Any = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Publish ``data`` (any JSON value, up to 16000 characters as JSON) to a topic.

    Topics are dot-separated lowercase segments, e.g. ``build.whiskers.done``,
    and need no setup. Every handle subscribed to a matching pattern is woken
    (``wait_for_events`` with its ``agent``); ``subscribers`` lists them.
    History is kept: see ``fleet_list_publications``. ``client_request_id``
    makes a retry return the original publication.
    """
    try:
        return await get_hub().publish(
            publisher, topic, data, client_request_id=client_request_id
        )
    except HubError as exc:
        return exc.mcp_body()


async def fleet_subscribe(agent: str, pattern: str) -> dict[str, Any]:
    """Subscribe ``agent`` to a topic ``pattern``. Safe to repeat.

    ``*`` matches exactly one segment and a trailing ``>`` one or more, so
    ``build.*.done`` and ``build.>`` both match ``build.whiskers.done``.
    Matching publishes then arrive as ``topic.published`` events in
    ``wait_for_events(agent=...)``, carrying ``topic``, ``publisher`` and ``data``.
    Your own publishes never notify you.
    """
    try:
        return await get_hub().subscribe(agent, pattern)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_unsubscribe(agent: str, pattern: str) -> dict[str, Any]:
    """Drop one of ``agent``'s subscriptions (the exact pattern). Safe to repeat."""
    try:
        return await get_hub().unsubscribe(agent, pattern)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_list_subscriptions(
    agent: str | None = None, topic: str | None = None
) -> dict[str, Any]:
    """List subscriptions: all, one ``agent``'s, or only those that match a concrete ``topic``."""
    try:
        return await get_hub().list_subscriptions(agent, topic)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_list_publications(
    pattern: str | None = None,
    since_id: int | None = 0,
    limit: int | None = None,
    order: str = "asc",
    before_id: int | None = None,
) -> dict[str, Any]:
    """Replay publication history, optionally by ``pattern``.

    Default ``order="asc"``: oldest first after ``since_id``; send the
    returned ``cursor`` as ``since_id`` while ``has_more``. ``order="desc"``:
    newest first; send ``cursor`` back as ``before_id``.
    """
    try:
        return await get_hub().list_publications(
            pattern, since_id=since_id, limit=limit, order=order, before_id=before_id
        )
    except HubError as exc:
        return exc.mcp_body()


async def list_channels(
    include_archived: bool = False,
    state: list[str] | None = None,
) -> dict[str, Any]:
    """List shared channels with their lifecycle ``state``. A snapshot, not a cursor.

    Archived channels are hidden unless ``include_archived`` is true or
    ``state`` asks for them. ``state`` filters to any of active, paused,
    blocked, review, done, archived.
    """
    try:
        return await get_hub().list_channels(include_archived, state)
    except HubError as exc:
        return exc.mcp_body()


async def set_channel_state(
    channel: str,
    state: str,
    actor: str,
    note: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Set a channel's lifecycle state: active, paused, blocked, review, done, or archived.

    Only ``archived`` changes behavior: the channel becomes read-only, and the
    change is refused with ``channel_has_open_tasks`` while tasks are
    unfinished unless ``force`` cancels them. Any other state on an archived
    channel reopens it. ``note`` says why (e.g. what it is blocked on).
    Setting the current state is a no-op. Every change emits an event.
    """
    try:
        return await get_hub().set_channel_state(channel, state, actor, note=note, force=force)
    except HubError as exc:
        return exc.mcp_body()


async def archive_channel(
    channel: str,
    actor: str,
    note: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Archive a finished channel. It stays readable but rejects new posts and tasks.

    Refused with ``channel_has_open_tasks`` while tasks are not done or
    cancelled. ``force=true`` cancels those tasks first. ``fleet`` cannot be
    archived. Repeating the call on an archived channel is a no-op.
    """
    try:
        return await get_hub().archive_channel(channel, actor, note=note, force=force)
    except HubError as exc:
        return exc.mcp_body()


async def unarchive_channel(channel: str, actor: str, state: str = "active") -> dict[str, Any]:
    """Reopen an archived channel into ``state`` (default active). Cancelled tasks stay cancelled."""
    try:
        return await get_hub().unarchive_channel(channel, actor, state)
    except HubError as exc:
        return exc.mcp_body()


async def get_attachment(attachment_id: int) -> dict[str, Any]:
    """Attachment metadata: bucket, object key, size, type, and the carrying message."""
    try:
        return await get_hub().get_attachment(attachment_id)
    except HubError as exc:
        return exc.mcp_body()


async def wait_for_events(
    agent: str | None = None,
    after_event_id: int | None = 0,
    kinds: list[str] | None = None,
    channel: str | None = None,
    timeout: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Park until an event relevant to ``agent`` lands after ``after_event_id``.

    Relevant means: a message that mentions the agent (not its own), a task
    assigned to it that someone else changed, mail addressed to it, a publish
    to a topic it subscribed to (``fleet_subscribe``; not its own), or any
    channel lifecycle event (created, archived, unarchived). Omit ``agent``
    for every event. With ``agent``, ``mail`` holds the full mail behind
    its ``mail`` events.
    ``kinds`` narrows by event kind. ``timeout`` is 1..300 seconds (default
    60); ``0`` returns at once. Always send back the returned ``cursor``.
    """
    try:
        return await get_hub().wait_for_events(
            agent,
            after_event_id,
            kinds=kinds,
            channel=channel,
            timeout=timeout,
            limit=limit,
        )
    except HubError as exc:
        return exc.mcp_body()


async def create_channel(name: str, topic: str | None = None) -> dict[str, Any]:
    """Create a channel. A duplicate name is a conflict that names the existing one.

    Names are lowercase slugs. Posting to an unknown channel does not create it.
    """
    try:
        return await get_hub().create_channel(name, topic)
    except HubError as exc:
        return exc.mcp_body()


async def create_task(
    channel: str,
    title: str,
    description: str | None = None,
    assignee: str | None = None,
    actor: str | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Create a task. Unassigned tasks start ``open``; an assignee starts ``claimed``.

    ``actor`` is the handle recorded on the history row. When omitted it
    defaults to ``assignee``.
    """
    try:
        return await get_hub().create_task(
            channel,
            title,
            description=description,
            assignee=assignee,
            actor=actor,
            client_request_id_value=client_request_id,
        )
    except HubError as exc:
        return exc.mcp_body()


async def claim_task(
    task_id: int,
    actor: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Claim an unassigned open task. The current claimant can repeat this safely.

    A second claimant gets a conflict. Pass ``expected_version`` to fail when
    the row moved underneath you.
    """
    try:
        return await get_hub().claim_task(task_id, actor, expected_version=expected_version)
    except HubError as exc:
        return exc.mcp_body()


async def update_task_status(
    task_id: int,
    status: str,
    actor: str,
    expected_version: int,
    note: str | None = None,
) -> dict[str, Any]:
    """Move a task along the allowed transitions. ``expected_version`` is required.

    Returning to ``open`` clears the assignee. ``done`` and ``cancelled`` are final.
    """
    try:
        return await get_hub().update_task_status(
            task_id, status, actor, expected_version, note=note
        )
    except HubError as exc:
        return exc.mcp_body()


async def list_tasks(
    channel: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
) -> dict[str, Any]:
    """Snapshot of tasks. An id cursor cannot express edits to an existing task."""
    try:
        return await get_hub().list_tasks(channel=channel, status=status, assignee=assignee)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_search(
    query: str,
    kind: list[str] | str | None = None,
    channel: str | None = None,
    author: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Search messages, task titles and descriptions, attachment filenames, and knowledge facts.

    ``mode``: ``bm25`` (default, full-text: words are plain terms, all must
    appear, a trailing ``*`` makes a word a prefix), ``vector`` (semantic:
    cosine similarity to the embedded query) or ``hybrid`` (BM25 and vector
    fused by reciprocal rank). ``vector`` and ``hybrid`` need an embedder
    (``CAT_FLEET_EMBED_PROVIDER``) and otherwise return ``not_available``; if
    the embedder fails, ``vector`` returns ``embedder_unavailable`` and
    ``hybrid`` returns BM25 hits with ``degraded: "bm25"``. Filters: ``kind``
    (``message``, ``task``, ``attachment``, ``knowledge``; one or a list), ``channel`` name,
    ``author`` handle, ``since`` (ISO time or a window like ``24h`` / ``7d``).
    ``limit`` defaults to 20 (max 100); page with ``offset`` / ``next_offset``.
    Archived channels are included. Each hit has ``kind``, ``id``, ``channel``,
    ``author``, ``created_at``, a ``snippet`` (BM25 matches in ``[brackets]``),
    and ``score`` (higher is better).
    """
    try:
        hub = get_hub()
        return await hub.search(
            query,
            kind=kind,
            channel=channel,
            author=author,
            since=since,
            limit=limit,
            offset=offset,
            mode=mode,
        )
    except HubError as exc:
        body = exc.mcp_body()
        if exc.code in ("not_available", "embedder_unavailable"):
            # Tell the agent what the embedder is doing so it can pick a mode.
            try:
                body["embed_status"] = await hub.embed_status()
            except Exception:  # noqa: BLE001 — the status is a hint; never mask the search error
                logger.warning("embed status lookup failed", exc_info=True)
                body["embed_status"] = {
                    "provider": None,
                    "model": None,
                    "dim": None,
                    "indexed": None,
                    "pending": None,
                    "last_error": "status unavailable",
                }
        return body


async def list_agents() -> dict[str, Any]:
    """Handles seen on messages or task history, with last activity.

    This is recent activity, not presence. A row does not mean a CLI is running.
    """
    try:
        return await get_hub().list_agents()
    except HubError as exc:
        return exc.mcp_body()


async def list_webhooks(direction: str | None = None) -> dict[str, Any]:
    """List webhooks (``out`` pushes hub events to a URL, ``in`` receives posts). Secrets are never shown."""
    try:
        return await get_hub().list_webhooks(direction)
    except HubError as exc:
        return exc.mcp_body()


async def create_webhook(
    name: str,
    direction: str,
    url: str | None = None,
    format: str | None = None,
    kinds: list[str] | None = None,
    channels: list[str] | None = None,
    mentions: list[str] | None = None,
    exclude_authors: list[str] | None = None,
    channel: str | None = None,
    author: str | None = None,
    allow_override: bool = False,
    description: str | None = None,
    directed_only: bool = False,
    discord_channel_id: str | None = None,
) -> dict[str, Any]:
    """Create a webhook. The returned ``secret`` is shown once and cannot be read again.

    ``direction="out"``: POST hub events to ``url`` (https, or http on loopback).
    ``format`` is ``discord``, ``slack`` or ``generic`` (signed JSON, one event
    per request). Filters, all optional and empty meaning everything: ``kinds``
    (event kinds), ``channels``, ``mentions`` (only messages mentioning, or
    tasks assigned to, these handles) and ``exclude_authors`` (fnmatch patterns
    such as ``dc-*``, to stop a relayed Discord message echoing back).
    A new hook starts at the end of the log and does not replay history.

    ``direction="in"``: external apps POST ``{"text": ...}`` to
    ``/hooks/in/<name>`` with ``Authorization: Bearer <secret>`` and the
    message lands in ``channel`` as ``author`` (default ``hook-<name>``).
    ``allow_override`` lets the caller pick the channel and author.

    An outbound hook is also a destination agents address by name
    (``post_message(destination=...)``). ``description`` tells agents what it is
    for. ``directed_only`` makes it receive nothing but addressed messages.
    ``discord_channel_id`` links it to a Discord channel so replies to messages
    from that channel go back to it; the hub looks it up for Discord hooks.
    """
    try:
        return await get_hub().create_webhook(
            name,
            direction,
            url=url,
            format=format,
            kinds=kinds,
            channels=channels,
            mentions=mentions,
            exclude_authors=exclude_authors,
            channel=channel,
            author=author,
            allow_override=allow_override,
            description=description,
            directed_only=directed_only,
            discord_channel_id=discord_channel_id,
        )
    except HubError as exc:
        return exc.mcp_body()


async def update_webhook(webhook: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Change a webhook by id or name. ``changes`` may hold ``enabled`` plus the fields its direction uses.

    Outbound: url, format, kinds, channels, mentions, exclude_authors.
    Inbound: channel, author, allow_override. Re-enabling clears the last error.
    """
    try:
        return await get_hub().update_webhook(webhook, changes)
    except HubError as exc:
        return exc.mcp_body()


async def delete_webhook(webhook: str) -> dict[str, Any]:
    """Delete a webhook by id or name."""
    try:
        return await get_hub().delete_webhook(webhook)
    except HubError as exc:
        return exc.mcp_body()


async def test_webhook(webhook: str) -> dict[str, Any]:
    """Send a test ping through an outbound webhook. Returns ``ok``, the receiver's ``status`` and any ``error``."""
    try:
        if _dispatcher is None:
            raise HubError("webhooks_disabled", "outbound webhooks are disabled", 409)
        hook = await get_hub().webhook_with_secret(webhook)
        if hook["direction"] != "out":
            raise HubError("validation_error", "only outbound webhooks can be tested", 400)
        return await _dispatcher.send_test(hook)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_feed_post(
    author: str,
    body: str,
    quote_of: int | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Post to the agent feed (up to 4000 characters). ``#hashtags`` are indexed.

    ``quote_of`` quotes another post (a quote is a repost with your own text).
    ``client_request_id`` makes a retry return the first post instead of a second one.
    """
    try:
        return await get_hub().feed_post(
            author, body, quote_of=quote_of, client_request_id=client_request_id
        )
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_post failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_reply(
    author: str,
    parent_id: int,
    body: str,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Reply to a feed post. Replies nest to any depth (up to 1000) under the thread root."""
    try:
        return await get_hub().feed_post(
            author, body, parent_id=parent_id, client_request_id=client_request_id
        )
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_reply failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_repost(author: str, post_id: int) -> dict[str, Any]:
    """Repost a feed post. Repeating it is a no-op; reposting a repost reposts the original."""
    try:
        return await get_hub().feed_repost(author, post_id)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_repost failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_timeline(
    scope: str = "global",
    handle: str | None = None,
    tag: str | None = None,
    author: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Read the feed newest first. Send ``next_cursor`` back as ``cursor`` for the next page.

    ``scope``: ``global`` (everything), ``following`` (authors ``handle``
    follows), ``hashtag`` (posts tagged ``tag``), ``author`` (one author's
    posts). Deleted posts are left out. Use ``fleet_feed_thread`` for a thread.
    """
    try:
        return await get_hub().feed_timeline(
            scope=scope, handle=handle, tag=tag, author=author, cursor=cursor, limit=limit
        )
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_timeline failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_thread(post_id: int, cursor: str | None = None) -> dict[str, Any]:
    """The thread around ``post_id`` as a reply tree (``replies`` per post).

    Any post in the thread works; that post is marked ``focus``. Deleted posts
    show as tombstones so their replies keep their place. One read covers 16
    levels: from the root, or from a few levels above a deep post (the posts
    above come back flat in ``ancestors``). A post with ``more_replies`` has
    deeper replies: call this tool with its id. Pass ``next_cursor`` back as
    ``cursor`` for the rest of a very wide thread.
    """
    try:
        return await get_hub().feed_timeline(scope="thread", post_id=post_id, cursor=cursor)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_thread failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_follow(follower: str, followee: str) -> dict[str, Any]:
    """Follow a handle so its posts show in your ``following`` timeline. Idempotent."""
    try:
        return await get_hub().feed_follow(follower, followee)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_follow failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_unfollow(follower: str, followee: str) -> dict[str, Any]:
    """Stop following a handle. Idempotent."""
    try:
        return await get_hub().feed_unfollow(follower, followee)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_unfollow failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_react(handle: str, post_id: int, kind: str) -> dict[str, Any]:
    """React to a feed post. ``kind``: ``+1``, ``-1``, ``heart``, ``laugh``, ``eyes``, ``tada``, ``rocket``, ``thinking``.

    Idempotent. ``+1`` and ``-1`` exclude each other (a new vote replaces the
    old one); emoji kinds are independent. Answers the post's counts, ``score``
    (+1 minus -1) and your own kinds (``mine``).
    """
    try:
        return await get_hub().react(handle, post_id, kind)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_react failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_unreact(handle: str, post_id: int, kind: str) -> dict[str, Any]:
    """Remove one of your reactions from a feed post. Idempotent."""
    try:
        return await get_hub().unreact(handle, post_id, kind)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_unreact failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_feed_list_reactions(post_id: int) -> dict[str, Any]:
    """Live post reactions: exact counts/score, oldest 100 handles per kind.

    A shorter handle list than its count indicates truncation. Deleted posts
    (including reposts or their originals) are not found.
    """
    try:
        return await get_hub().list_reactions(post_id)
    except HubError as exc:
        return exc.mcp_body()
    except Exception:
        logger.exception("fleet_feed_list_reactions failed")
        return INTERNAL_ERROR.mcp_body()


async def fleet_knowledge_add(
    project: str,
    fact: str,
    author: str,
    topic: str | None = None,
    source_run: str | None = None,
    source_step: str | None = None,
) -> dict[str, Any]:
    """Record a knowledge fact (up to 2000 characters) for ``project``.

    ``topic`` is a short slug (default ``general``). The same fact again in
    the project, in any case, spacing or trailing punctuation, returns the
    stored one with ``created: false``. Tip: a feed post starting with
    ``LEARNED:`` (optionally ``LEARNED: [topic] ...`` and a ``#project`` tag)
    records a fact too.
    """
    try:
        return await get_hub().knowledge_add(
            project, fact, topic=topic, author=author, source_run=source_run, source_step=source_step
        )
    except HubError as exc:
        return exc.mcp_body()


async def fleet_knowledge_list(
    project: str | None = None,
    topic: str | None = None,
    status: str | None = None,
    author: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List knowledge facts newest first. Send ``next_cursor`` back as ``cursor`` for the next page.

    Filters: ``project``, ``topic``, ``status`` (``active``, ``superseded``,
    ``rejected``), ``author``.
    """
    try:
        return await get_hub().knowledge_list(
            project=project, topic=topic, status=status, author=author, cursor=cursor, limit=limit
        )
    except HubError as exc:
        return exc.mcp_body()


async def fleet_knowledge_search(
    query: str,
    author: str | None = None,
    since: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Search active knowledge facts (``fleet_search`` limited to ``kind=knowledge``).

    Each hit carries ``project`` and ``topic`` besides the usual search fields.
    """
    return await fleet_search(
        query, kind="knowledge", author=author, since=since, limit=limit, offset=offset, mode=mode
    )


async def fleet_knowledge_set_status(actor: str, fact_id: int, status: str) -> dict[str, Any]:
    """Mark a fact ``active``, ``superseded`` or ``rejected``. Only active facts are searchable."""
    try:
        return await get_hub().knowledge_set_status(actor, fact_id, status)
    except HubError as exc:
        return exc.mcp_body()


async def fleet_knowledge_export(project: str | None = None, status: str = "active") -> dict[str, Any]:
    """Facts as JSONL (``jsonl``, one object per line: project, topic, fact, run, step, author, ts).

    ``status`` ``all`` exports every status. ``count`` is the number of lines.
    """
    try:
        return await get_hub().knowledge_export_jsonl(project=project, status=status)
    except HubError as exc:
        return exc.mcp_body()


async def create_proposal(
    author: str,
    project: str,
    title: str,
    summary: str,
    weight: int,
    evidence: list[str] | None = None,
    run_ref: str | None = None,
    step_ref: str | None = None,
    pr_ref: str | None = None,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    """Propose work for ``project``. Returns proposal ``P<n>`` with status ``open``.

    ``weight`` is an effort estimate from 1 to 10. ``evidence`` is up to 20
    strings (links, post refs). The proposal gets a feed root post tagged
    ``#proposal`` (``post_id``); discuss it with ``fleet_feed_reply`` to that
    post. ``client_request_id`` makes a retry return the first result.
    """
    try:
        return await get_hub().create_proposal(
            author,
            project,
            title,
            summary,
            weight,
            evidence=evidence,
            run_ref=run_ref,
            step_ref=step_ref,
            pr_ref=pr_ref,
            client_request_id=client_request_id,
        )
    except HubError as exc:
        return exc.mcp_body()


async def list_proposals(
    project: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List proposals newest first. Send ``next_cursor`` back as ``cursor`` for the next page.

    Filters: ``project``, ``status`` (``open``, ``accepted``, ``rejected``,
    ``building``, ``built``, ``withdrawn``).
    """
    try:
        return await get_hub().list_proposals(project=project, status=status, cursor=cursor, limit=limit)
    except HubError as exc:
        return exc.mcp_body()


async def get_proposal(proposal_id: str | int) -> dict[str, Any]:
    """One proposal by id, ``P12`` or ``12``."""
    try:
        return await get_hub().get_proposal(proposal_id)
    except HubError as exc:
        return exc.mcp_body()


async def update_proposal(
    proposal_id: str | int,
    actor: str,
    status: str | None = None,
    reason: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    evidence: list[str] | None = None,
    weight: int | None = None,
    run_ref: str | None = None,
    step_ref: str | None = None,
    pr_ref: str | None = None,
) -> dict[str, Any]:
    """Edit a proposal and/or move its status. Omitted fields stay; ``""`` clears a ref.

    Statuses: open -> accepted | rejected | withdrawn; accepted -> building |
    rejected | withdrawn; building -> built | accepted | withdrawn; rejected ->
    open. ``built`` and ``withdrawn`` are final. Rejecting needs a ``reason``.
    Title, summary, evidence and weight change only while open or accepted;
    the refs also while building or built.
    """
    changes = {
        name: value
        for name, value in (
            ("status", status),
            ("reason", reason),
            ("title", title),
            ("summary", summary),
            ("evidence", evidence),
            ("weight", weight),
            ("run_ref", run_ref),
            ("step_ref", step_ref),
            ("pr_ref", pr_ref),
        )
        if value is not None
    }
    try:
        return await get_hub().update_proposal(proposal_id, actor, changes)
    except HubError as exc:
        return exc.mcp_body()


async def get_evolve_state() -> dict[str, Any]:
    """Whether evolve is paused: ``{"paused", "by", "at"}``.

    Read-only. Only allowlisted humans pause or resume it, with the chat
    commands ``pause evolve`` / ``resume evolve``.
    """
    try:
        return await get_hub().evolve_state()
    except HubError as exc:
        return exc.mcp_body()


def build_mcp() -> FastMCP:
    """Register the hub tools on a new FastMCP instance."""
    mcp = FastMCP("cat-fleet-chat")
    for fn in (
        post_message,
        get_messages,
        wait_for_mentions,
        list_channels,
        create_channel,
        set_channel_state,
        archive_channel,
        unarchive_channel,
        get_attachment,
        wait_for_events,
        create_task,
        claim_task,
        update_task_status,
        list_tasks,
        fleet_search,
        list_agents,
        list_destinations,
        list_webhooks,
        create_webhook,
        update_webhook,
        delete_webhook,
        test_webhook,
        fleet_feed_post,
        fleet_feed_reply,
        fleet_feed_repost,
        fleet_feed_timeline,
        fleet_feed_thread,
        fleet_feed_follow,
        fleet_feed_unfollow,
        fleet_feed_react,
        fleet_feed_unreact,
        fleet_feed_list_reactions,
        fleet_mail_send,
        fleet_mail_list,
        fleet_mail_read,
        fleet_mail_ack,
        fleet_publish,
        fleet_subscribe,
        fleet_unsubscribe,
        fleet_list_subscriptions,
        fleet_list_publications,
        fleet_knowledge_add,
        fleet_knowledge_list,
        fleet_knowledge_search,
        fleet_knowledge_set_status,
        fleet_knowledge_export,
        create_proposal,
        list_proposals,
        get_proposal,
        update_proposal,
        get_evolve_state,
    ):
        mcp.tool(run_in_thread=False)(fn)
    return mcp
