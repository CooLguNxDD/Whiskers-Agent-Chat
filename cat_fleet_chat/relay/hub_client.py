"""The relay's HTTP client for the hub, authenticated with its scoped ``rly_`` token.

Every call answers ``(ok, body_or_reason)`` instead of raising, so a hub
outage degrades the relay (a failure mark, a retry) and never crashes the
platform client. Writes that are safe to retry (idempotent by
``client_request_id``, or idempotent by nature) retry network errors and
5xx; other calls try once.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

ATTEMPTS = 3
RETRY_DELAY = 2.0
TIMEOUT = 15.0
CHANNELS_TTL = 30.0


def _reason(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {response.status_code}"


class HubClient:
    """Thin wrapper over the hub REST API for one relay."""

    def __init__(self, client: httpx.AsyncClient, url: str, token: str, relay: str) -> None:
        self.client = client
        self.base = url.rstrip("/")
        self.relay = relay
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._channels: dict[str, Any] = {"at": 0.0, "names": None}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        retry: bool = False,
        timeout: float = TIMEOUT,
    ) -> tuple[bool, Any]:
        """One API call. ``(True, body)`` on 2xx, else ``(False, reason)``."""
        reason = "unknown error"
        for attempt in range(ATTEMPTS if retry else 1):
            if attempt:
                await asyncio.sleep(RETRY_DELAY * attempt)
            try:
                response = await self.client.request(
                    method, self.base + path, json=json, params=params, headers=self.headers, timeout=timeout
                )
            except httpx.HTTPError as exc:
                reason = f"hub unreachable ({type(exc).__name__})"
                continue
            if response.is_success:
                try:
                    return True, response.json()
                except ValueError:
                    return True, {}
            reason = _reason(response)
            if response.status_code < 500:
                break
        return False, reason

    # ---- relay control plane ------------------------------------------------

    async def config(self) -> tuple[bool, Any]:
        return await self.request("GET", f"/api/v1/relays/{self.relay}/config")

    async def heartbeat(self, status: dict[str, Any]) -> tuple[bool, Any]:
        return await self.request("POST", f"/api/v1/relays/{self.relay}/heartbeat", json={"status": status})

    async def reply_target(self, message_id: int) -> tuple[bool, Any]:
        return await self.request("GET", f"/api/v1/relays/{self.relay}/reply-target/{int(message_id)}")

    async def consumer_wait(self, name: str, timeout: int) -> tuple[bool, Any]:
        return await self.request(
            "GET",
            f"/api/v1/consumers/{name}/wait",
            params={"timeout": timeout, "limit": 20},
            timeout=timeout + 15,
        )

    async def consumer_ack(self, name: str, cursor: int) -> tuple[bool, Any]:
        return await self.request("POST", f"/api/v1/consumers/{name}/ack", json={"cursor": cursor}, retry=True)

    async def add_binding(self, body: dict[str, Any]) -> tuple[bool, Any]:
        return await self.request("POST", f"/api/v1/relays/{self.relay}/bindings", json=body)

    async def delete_binding(self, binding_id: int, actor: str) -> tuple[bool, Any]:
        return await self.request(
            "DELETE", f"/api/v1/relays/{self.relay}/bindings/{int(binding_id)}", params={"actor": actor}
        )

    async def add_route(self, body: dict[str, Any]) -> tuple[bool, Any]:
        return await self.request("POST", f"/api/v1/relays/{self.relay}/routes", json=body)

    async def delete_route(self, route_id: int, actor: str) -> tuple[bool, Any]:
        return await self.request(
            "DELETE", f"/api/v1/relays/{self.relay}/routes/{int(route_id)}", params={"actor": actor}
        )

    # ---- fleet ------------------------------------------------------------------

    async def post_message(self, payload: dict[str, Any]) -> tuple[bool, Any]:
        """POST a message. ``client_request_id`` makes the retries safe."""
        return await self.request("POST", "/api/v1/messages", json=payload, retry=True)

    async def channels(self) -> set[str] | None:
        """Open hub channel names, cached; ``None`` when the hub was never reachable."""
        now = time.monotonic()
        if self._channels["names"] is not None and now - self._channels["at"] < CHANNELS_TTL:
            return self._channels["names"]
        ok, body = await self.request("GET", "/api/v1/channels")
        if not ok:
            return self._channels["names"]  # a stale list beats none
        try:
            names = {item["name"] for item in body["channels"]}
        except (KeyError, TypeError):
            return self._channels["names"]
        self._channels.update(at=now, names=names)
        return names

    async def feed_react(self, post_id: int, kind: str, handle: str) -> tuple[bool, Any]:
        return await self.request(
            "PUT", f"/api/v1/feed/posts/{int(post_id)}/reactions/{kind}", json={"handle": handle}, retry=True
        )

    async def claim_task(self, task_id: int, actor: str, version: int | None = None) -> tuple[bool, Any]:
        body: dict[str, Any] = {"actor": actor}
        if version is not None:
            body["expected_version"] = version
        return await self.request("POST", f"/api/v1/tasks/{int(task_id)}/claim", json=body)

    async def task_status(
        self, task_id: int, actor: str, status: str, version: int, note: str | None = None
    ) -> tuple[bool, Any]:
        """Move a task; ``version`` is the one the button was rendered from, so a stale click is refused."""
        body: dict[str, Any] = {"actor": actor, "status": status, "expected_version": version}
        if note:
            body["note"] = note
        return await self.request("POST", f"/api/v1/tasks/{int(task_id)}/status", json=body)

    async def get(self, path: str, **params: Any) -> tuple[bool, Any]:
        return await self.request("GET", path, params={k: v for k, v in params.items() if v not in (None, "")})
