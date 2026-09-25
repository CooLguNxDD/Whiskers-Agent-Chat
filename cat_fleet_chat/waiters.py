"""In-process wakeups for long-poll and SSE.

Writers commit, drop the database writer lock, then notify. Waiters hold this
condition across the empty-check and the sleep, so a mention that lands in
that gap cannot be missed. One condition serves every cursor: each waiter
re-queries its own filter after a wake.
"""

from __future__ import annotations

import asyncio


class WaitHub:
    def __init__(self) -> None:
        self.condition = asyncio.Condition()

    async def notify(self) -> None:
        async with self.condition:
            self.condition.notify_all()

    @property
    def waiter_count(self) -> int:
        """Parked waiters. Cancellation must leave this at the pre-wait value."""
        return len(self.condition._waiters)  # noqa: SLF001 — test/observability only
