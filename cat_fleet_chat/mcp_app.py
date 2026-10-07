"""Standalone FastMCP tools. Same operations as the REST API, different envelope on errors."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from cat_fleet_chat.errors import HubError
from cat_fleet_chat.hub import Hub

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
) -> dict[str, Any]:
    """Post a message into a channel.

    Mentions are @handles at token boundaries (not email addresses) and are
    stored at write time. ``client_request_id`` makes a retry return the
    original message instead of posting a second one. A different payload
    with the same key is a conflict.

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
        )
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
) -> dict[str, Any]:
    """Park until ``agent`` is mentioned in a message with id greater than ``since_id``.

    ``timeout`` is clamped to 1..300 seconds (default 60). An empty result
    keeps the cursor you passed and sets ``timed_out`` true. Drain ``has_more``
    pages before waiting again. Pass a shorter timeout when the MCP client
    cuts tools off sooner than the hub would return.
    """
    try:
        return await get_hub().wait_for_mentions(
            agent, since_id, channel=channel, timeout=timeout, limit=limit
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
    assigned to it that someone else changed, or any channel lifecycle event
    (created, archived, unarchived). Omit ``agent`` for every event.
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
        list_agents,
        list_webhooks,
        create_webhook,
        update_webhook,
        delete_webhook,
        test_webhook,
    ):
        mcp.tool(run_in_thread=False)(fn)
    return mcp
