"""Optional text embeddings: providers, vector packing, and the background embed queue.

The hub never needs an embedder. With ``CAT_FLEET_EMBED_PROVIDER=off`` (the
default) :class:`NullEmbedder` is used, the queue does nothing, and search
stays BM25 only.

Vectors are stored as float32 little-endian blobs (:func:`pack`) and are
L2-normalised before they are written, so cosine similarity is a plain dot
product (:func:`dot`). Scoring is pure Python; no numpy.

Nothing here logs an API key or the text being embedded. Error strings carry
only the exception type, an HTTP status, or counts.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import sys
from array import array
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from cat_fleet_chat import store

logger = logging.getLogger("cat_fleet_chat.embed")

# Longest text sent for one row. Long messages keep their opening, which is
# where the topic usually is.
EMBED_TEXT_MAX = 8_000
# Rows the startup rescan reads per page when it looks for edited text.
RESCAN_PAGE = 500
# Backoff after a failed batch: BACKOFF_BASE * 2**(failures-1), capped.
BACKOFF_BASE = 1.0
BACKOFF_MAX = 300.0
# How long the queue sleeps when there is nothing to embed, unless woken.
IDLE_WAIT = 30.0


class EmbedderError(Exception):
    """The embedder could not produce vectors. The message is safe to show and log."""


class Embedder(Protocol):
    """What the queue and search need from a provider.

    ``name`` is the model key stored with each vector. ``dim`` is the vector
    width, ``None`` until the first successful call tells us.
    """

    provider: str
    name: str
    dim: int | None
    timeout: float

    @property
    def enabled(self) -> bool: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


# ---- vectors ---------------------------------------------------------------


def normalize(vec: Iterable[float]) -> list[float]:
    """``vec`` scaled to unit length. A zero vector stays zero."""
    values = [float(x) for x in vec]
    norm = math.sqrt(sum(x * x for x in values))
    if norm == 0.0 or not math.isfinite(norm):
        return [0.0] * len(values)
    return [x / norm for x in values]


def pack(vec: Iterable[float]) -> bytes:
    """float32 little-endian bytes."""
    data = array("f", (float(x) for x in vec))
    if sys.byteorder != "little":
        data.byteswap()
    return data.tobytes()


def unpack(blob: bytes) -> list[float]:
    """Inverse of :func:`pack`."""
    data = array("f")
    data.frombytes(bytes(blob))
    if sys.byteorder != "little":
        data.byteswap()
    return data.tolist()


def dot(a: Iterable[float], b: Iterable[float]) -> float:
    """Dot product; equals cosine similarity for unit vectors."""
    return math.fsum(x * y for x, y in zip(a, b))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def entry_text(row: dict[str, Any]) -> str:
    """The text embedded for one search_index entry: title, then body."""
    parts = [str(row.get("title") or "").strip(), str(row.get("body") or "").strip()]
    joined = "\n".join(part for part in parts if part)
    return (joined or str(row.get("kind") or ""))[:EMBED_TEXT_MAX]


def safe_error(exc: BaseException) -> str:
    """A short description of ``exc`` without request bodies, keys or response text."""
    if isinstance(exc, EmbedderError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    return type(exc).__name__


# ---- providers -------------------------------------------------------------


class NullEmbedder:
    """Provider ``off``: no vectors. Vector and hybrid search stay unavailable."""

    provider = "off"
    name = ""
    dim: int | None = None
    timeout = 0.0

    @property
    def enabled(self) -> bool:
        return False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedderError("no embedder is configured")

    async def aclose(self) -> None:
        return None


def _endpoint(base: str) -> str:
    """``{base}/v1/embeddings``, unless ``base`` already names the endpoint or ``/v1``."""
    url = base.rstrip("/")
    if url.endswith("/v1/embeddings"):
        return url
    if url.endswith("/v1"):
        return url + "/embeddings"
    return url + "/v1/embeddings"


class _HTTPEmbedder:
    """Shared HTTP plumbing. Subclasses build the body and parse the reply."""

    provider = ""

    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = _endpoint(url)
        self.name = model
        self.dim: int | None = None
        self.timeout = float(timeout)
        self._api_key = api_key
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self.provider!r}, model={self.name!r})"

    @property
    def enabled(self) -> bool:
        return True

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, texts: list[str]) -> dict[str, Any]:
        raise NotImplementedError

    def _vectors(self, payload: Any) -> list[list[float]]:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        try:
            response = await self._client.post(self.url, json=self._body(texts), headers=self._headers())
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbedderError(f"{self.provider} embedder failed: {safe_error(exc)}") from None
        try:
            vectors = [[float(x) for x in vec] for vec in self._vectors(payload)]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise EmbedderError(f"{self.provider} embedder returned an unexpected payload") from None
        if len(vectors) != len(texts):
            raise EmbedderError(
                f"{self.provider} embedder returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        widths = {len(vec) for vec in vectors}
        if len(widths) != 1 or 0 in widths:
            raise EmbedderError(f"{self.provider} embedder returned vectors of uneven width")
        self.dim = widths.pop()
        return vectors

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _openai_vectors(payload: Any) -> list[list[float]]:
    data = sorted(payload["data"], key=lambda item: int(item.get("index", 0)))
    return [item["embedding"] for item in data]


class OpenAIEmbedder(_HTTPEmbedder):
    """OpenAI-compatible ``POST {base}/v1/embeddings`` (LM Studio, vLLM, llama.cpp server...)."""

    provider = "openai"

    def _body(self, texts: list[str]) -> dict[str, Any]:
        return {"model": self.name, "input": list(texts)}

    def _vectors(self, payload: Any) -> list[list[float]]:
        return _openai_vectors(payload)


class ContentPartsEmbedder(_HTTPEmbedder):
    """Local embed server speaking "content-parts v1". Text parts only for now.

    Each input is ``{"content": [{"type": "text", "text": ...}]}``. The reply
    may be OpenAI-shaped (``data[].embedding``) or ``{"embeddings": [[...]]}``.
    """

    provider = "content-parts"

    def _body(self, texts: list[str]) -> dict[str, Any]:
        return {
            "model": self.name,
            "input": [{"content": [{"type": "text", "text": text}]} for text in texts],
        }

    def _vectors(self, payload: Any) -> list[list[float]]:
        if isinstance(payload, dict) and "embeddings" in payload:
            return list(payload["embeddings"])
        return _openai_vectors(payload)


def build_embedder(settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> Embedder:
    """The embedder ``settings`` asks for. ``off`` (or a missing URL/model) gives :class:`NullEmbedder`."""
    provider = getattr(settings, "embed_provider", "off") or "off"
    url = getattr(settings, "embed_url", None)
    model = getattr(settings, "embed_model", None)
    if provider == "off" or not url or not model:
        return NullEmbedder()
    kwargs = {
        "api_key": getattr(settings, "embed_api_key", None),
        "timeout": getattr(settings, "embed_timeout", 20.0),
        "transport": transport,
    }
    if provider == "openai":
        return OpenAIEmbedder(url, model, **kwargs)
    if provider == "content-parts":
        return ContentPartsEmbedder(url, model, **kwargs)
    raise ValueError(f"unknown embed provider {provider!r}")


# ---- queue -----------------------------------------------------------------


def backoff_delay(failures: int, base: float = BACKOFF_BASE, cap: float = BACKOFF_MAX) -> float:
    return min(cap, base * (2 ** max(0, failures - 1)))


class EmbedQueue:
    """Background task that gives every search_index entry a vector for the current model.

    It picks up entries with no vector (new rows, a new model, a new width)
    and entries :meth:`mark`-ed after a write whose text hash changed. Provider
    calls happen outside the write lock; vectors are written in one short
    transaction per batch. Failures back off exponentially and never stop the
    loop. ``sleep`` is injectable so tests can observe the backoff.
    """

    def __init__(
        self,
        db: Any,
        embedder: Embedder,
        *,
        batch: int = 16,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        backoff_base: float = BACKOFF_BASE,
        backoff_max: float = BACKOFF_MAX,
        idle_wait: float = IDLE_WAIT,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.batch = max(1, int(batch))
        self._sleep = sleep
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.idle_wait = idle_wait
        self.failures = 0
        self.last_error: str | None = None
        self._dirty: set[tuple[str, int]] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._rescanned = False

    @property
    def active(self) -> bool:
        return bool(self.embedder.enabled) and bool(getattr(self.db, "fts5_available", False))

    def mark(self, keys: Iterable[tuple[str, int]]) -> None:
        """Entries whose text may have changed. Call after the write committed."""
        if not self.active:
            return
        added = False
        for kind, ref_id in keys:
            self._dirty.add((kind, int(ref_id)))
            added = True
        if added:
            self._wake.set()

    def start(self) -> None:
        if self._task is None and self.active:
            self._task = asyncio.create_task(self._run(), name="cat-fleet-embed-queue")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def rescan(self) -> int:
        """Mark embedded entries whose text changed while nobody was watching. Returns the count."""
        model = self.embedder.name
        after = 0
        stale = 0
        while True:
            rows = await self.db.read(
                lambda conn, after=after: store.embedded_page(conn, model, after, RESCAN_PAGE)
            )
            if not rows:
                break
            for row in rows:
                if text_hash(entry_text(row)) != row["text_hash"]:
                    self._dirty.add((row["kind"], int(row["ref_id"])))
                    stale += 1
            after = int(rows[-1]["rowid"])
        return stale

    async def run_once(self) -> int:
        """Embed at most one batch. Returns how many vectors were written (0 = nothing to do)."""
        if not self.active:
            return 0
        if not self._rescanned:
            await self.rescan()
            self._rescanned = True
        model = self.embedder.name
        dim = self.embedder.dim
        todo: dict[tuple[str, int], dict[str, Any]] = {}

        if self._dirty:
            keys = list(self._dirty)
            rows = await self.db.read(lambda conn: store.index_entries(conn, keys, model))
            present = set()
            for row in rows:
                key = (row["kind"], int(row["ref_id"]))
                present.add(key)
                if len(todo) >= self.batch:
                    continue
                body = entry_text(row)
                digest = text_hash(body)
                stale_dim = dim is not None and row["dim"] is not None and int(row["dim"]) != dim
                if row["text_hash"] != digest or stale_dim:
                    todo[key] = {"text": body, "hash": digest}
                else:
                    self._dirty.discard(key)
            # Deleted sources have no index row left: nothing to embed.
            self._dirty.difference_update(set(keys) - present)

        if len(todo) < self.batch:
            missing = await self.db.read(
                lambda conn: store.embed_missing(conn, model, dim, self.batch)
            )
            for row in missing:
                key = (row["kind"], int(row["ref_id"]))
                if key in todo or len(todo) >= self.batch:
                    continue
                body = entry_text(row)
                todo[key] = {"text": body, "hash": text_hash(body)}

        if not todo:
            return 0
        keys = list(todo)
        vectors = await self.embedder.embed([todo[key]["text"] for key in keys])
        if len(vectors) != len(keys):
            raise EmbedderError(f"embedder returned {len(vectors)} vectors for {len(keys)} inputs")
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {
                "kind": kind,
                "ref_id": ref_id,
                "model": model,
                "dim": len(vec),
                "vector": pack(normalize(vec)),
                "text_hash": todo[(kind, ref_id)]["hash"],
                "created_at": now,
            }
            for (kind, ref_id), vec in zip(keys, vectors)
        ]

        async def write(conn):
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await store.upsert_embeddings(conn, rows)
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()

        await self.db.write(write)
        self._dirty.difference_update(keys)
        return len(rows)

    async def drain(self, max_batches: int = 1_000) -> int:
        """Run batches until there is nothing left. For tests and tools; raises on failure."""
        total = 0
        for _ in range(max_batches):
            done = await self.run_once()
            if not done:
                break
            total += done
        return total

    async def _run(self) -> None:
        while True:
            try:
                done = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the queue must outlive any provider error
                self.failures += 1
                self.last_error = safe_error(exc)
                delay = backoff_delay(self.failures, self.backoff_base, self.backoff_max)
                logger.warning(
                    "embed batch failed (%s); retry %d in %.1fs", self.last_error, self.failures, delay
                )
                await self._sleep(delay)
                continue
            if self.failures:
                logger.info("embedder recovered after %d failures", self.failures)
            self.failures = 0
            self.last_error = None
            if done:
                await asyncio.sleep(0)
                continue
            self._wake.clear()
            if self._dirty:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), self.idle_wait)
            except asyncio.TimeoutError:
                pass
