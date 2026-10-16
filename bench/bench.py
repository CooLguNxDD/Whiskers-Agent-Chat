"""Cat Fleet Chat benchmark: post throughput, search latency, long-poll waiters, feed reads.

Run from the repository root::

    uv run --extra dev python bench/bench.py            # full run, 100k messages
    uv run --extra dev python bench/bench.py --quick    # ~2k messages, seconds

It drives ``Hub`` and ``Database`` in process on a temporary SQLite file: no
network, no real embedder, synthetic data only. The temporary directory is
removed on exit and nothing is written to the default data directory. The
report names the CPU count, Python and SQLite versions and the OS family,
never a hostname or a path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import sqlite3
import sys
import tempfile
import time
import zlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Allow `python bench/bench.py` from a checkout that is not pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cat_fleet_chat import embed, schema  # noqa: E402
from cat_fleet_chat.db import Database  # noqa: E402
from cat_fleet_chat.hub import Hub  # noqa: E402
from cat_fleet_chat.waiters import WaitHub  # noqa: E402

# Targets from bench/README.md, checked against a full (100k message) run.
TARGETS = {
    "post_sequential": ("posts/s", ">=", 500.0),
    "search_bm25_p95": ("ms", "<", 50.0),
    "search_hybrid_p95": ("ms", "<", 250.0),
    "waiters_wake_p95": ("ms", "<", 200.0),
}
FULL_MESSAGES = 100_000
WAITER_TARGET_COUNT = 50  # the waiter target only counts with this many parked, none lost or woken early
CHANNELS = 6
AGENTS = 24
VOCAB_SIZE = 2_000
EMBED_DIM = 384
SEED_CHUNK = 5_000
EMBED_CHUNK = 1_000


# ---- measuring ---------------------------------------------------------------


def percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(fraction * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(latencies_ms: list[float], wall_s: float) -> dict[str, Any]:
    """Count, wall time, throughput and p50/p95/p99 for one scenario."""
    ordered = sorted(latencies_ms)
    count = len(ordered)
    return {
        "count": count,
        "wall_s": round(wall_s, 4),
        "throughput_per_s": round(count / wall_s, 2) if wall_s > 0 else 0.0,
        "mean_ms": round(sum(ordered) / count, 3) if count else 0.0,
        "p50_ms": round(percentile(ordered, 0.50), 3),
        "p95_ms": round(percentile(ordered, 0.95), 3),
        "p99_ms": round(percentile(ordered, 0.99), 3),
        "max_ms": round(ordered[-1], 3) if ordered else 0.0,
    }


async def timed(call: Callable[[], Awaitable[Any]]) -> tuple[float, Any]:
    start = time.perf_counter()
    result = await call()
    return (time.perf_counter() - start) * 1000.0, result


# ---- synthetic data ----------------------------------------------------------

_SYLLABLES = (
    "ba", "ko", "ri", "ten", "mur", "la", "vo", "zi", "dre", "san", "pel", "fu",
    "gar", "nim", "ox", "yu", "qua", "sho", "tri", "wen", "hal", "jek", "cor", "bly",
)  # fmt: skip


class Corpus:
    """Deterministic vocabulary with a Zipf-like word frequency."""

    def __init__(self, rng: random.Random) -> None:
        words: list[str] = []
        seen: set[str] = set()
        while len(words) < VOCAB_SIZE:
            word = "".join(rng.choice(_SYLLABLES) for _ in range(rng.choice((2, 3, 3, 4))))
            if word not in seen:
                seen.add(word)
                words.append(word)
        self.words = words
        total = 0.0
        self.cum_weights: list[float] = []
        for rank in range(VOCAB_SIZE):
            total += 1.0 / (rank + 1)
            self.cum_weights.append(total)
        self.rng = rng

    def text(self) -> str:
        count = self.rng.randint(8, 30)
        return " ".join(self.rng.choices(self.words, cum_weights=self.cum_weights, k=count))

    def query(self) -> str:
        terms = self.rng.choices((1, 2, 3), weights=(4, 4, 2))[0]
        return " ".join(self.rng.choices(self.words, cum_weights=self.cum_weights, k=terms))


class FakeEmbedder:
    """Deterministic hash embedder: no model, no network, bag-of-words buckets.

    Satisfies ``embed.Embedder``, so the hub takes it through its public
    ``embedder`` attribute and searches by its vectors like a real provider's.
    """

    provider = "bench-fake"
    name = "bench-fake-v1"
    timeout = 5.0

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim
        self._slots: dict[str, tuple[int, int, float, float]] = {}

    @property
    def enabled(self) -> bool:
        return True

    def _slot(self, word: str) -> tuple[int, int, float, float]:
        slot = self._slots.get(word)
        if slot is None:
            raw = word.encode("utf-8")
            first, second = zlib.crc32(raw), zlib.crc32(raw, 0x9E3779B9)
            slot = (
                first % self.dim,
                second % self.dim,
                1.0 if first & 0x10000 else -1.0,
                1.0 if second & 0x10000 else -1.0,
            )
            self._slots[word] = slot
        return slot

    def vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in text.lower().split():
            a, b, sign_a, sign_b = self._slot(word)
            vec[a] += sign_a
            vec[b] += sign_b
        return embed.normalize(vec)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]

    async def aclose(self) -> None:
        return None


# ---- scenarios ---------------------------------------------------------------


async def scenario_seed(
    hub: Hub, corpus: Corpus, embedder: FakeEmbedder, messages: int, rng: random.Random
) -> dict[str, Any]:
    """Bulk-insert ``messages`` rows, build the FTS index and the vectors. Reported in phases."""
    channel_ids = []
    for index in range(CHANNELS):
        created = await hub.create_channel(f"bench-{index}")
        channel_ids.append(created["channel"]["id"])
    agents = [f"bench-agent-{index:02d}" for index in range(AGENTS)]
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    texts: list[str] = []

    async def insert(conn):
        await conn.execute("BEGIN IMMEDIATE")
        for start in range(0, messages, SEED_CHUNK):
            rows = []
            mention_rows = []
            for offset in range(min(SEED_CHUNK, messages - start)):
                number = start + offset + 1
                author = agents[rng.randrange(AGENTS)]
                text = corpus.text()
                if rng.random() < 0.05:
                    target = agents[rng.randrange(AGENTS)]
                    text = f"@{target} {text}"
                    mention_rows.append((number, target))
                texts.append(text)
                stamp = (base + timedelta(seconds=number)).isoformat()
                rows.append((channel_ids[number % CHANNELS], author, text, stamp))
            await conn.executemany(
                "INSERT INTO messages (channel_id, author, text, created_at) VALUES (?, ?, ?, ?)", rows
            )
            await conn.executemany(
                "INSERT INTO mentions (message_id, agent_name) VALUES (?, ?)", mention_rows
            )
        await conn.commit()

    started = time.perf_counter()
    await hub.db.write(insert)
    insert_s = time.perf_counter() - started

    async def index(conn):
        await conn.execute("BEGIN IMMEDIATE")
        for statement in schema.search_backfill_sql():
            await conn.execute(statement)
        await conn.commit()

    started = time.perf_counter()
    await hub.db.write(index)
    index_s = time.perf_counter() - started

    started = time.perf_counter()
    await seed_vectors(hub, embedder, texts)
    vectors_s = time.perf_counter() - started
    total_s = insert_s + index_s + vectors_s
    return {
        "messages": messages,
        "wall_s": round(total_s, 3),
        "throughput_per_s": round(messages / total_s, 1),
        "insert_s": round(insert_s, 3),
        "fts_index_s": round(index_s, 3),
        "vectors_s": round(vectors_s, 3),
        "channels": CHANNELS,
        "agents": AGENTS,
        "vocabulary": VOCAB_SIZE,
        "embed_dim": embedder.dim,
    }


async def seed_vectors(hub: Hub, embedder: FakeEmbedder, texts: list[str]) -> None:
    """Write one vector per seeded message directly, as the embed queue would have."""
    now = datetime.now(timezone.utc).isoformat()

    async def write(conn):
        await conn.execute("BEGIN IMMEDIATE")
        for start in range(0, len(texts), EMBED_CHUNK):
            rows = []
            for offset, text in enumerate(texts[start : start + EMBED_CHUNK]):
                rows.append(
                    (
                        "message",
                        start + offset + 1,
                        embedder.name,
                        embedder.dim,
                        embed.pack(embedder.vector(text)),
                        embed.text_hash(embed.entry_text({"body": text})),
                        now,
                    )
                )
            await conn.executemany(
                "INSERT INTO embeddings (kind, ref_id, model, dim, vector, text_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        await conn.commit()

    await hub.db.write(write)


async def scenario_post(
    hub: Hub, corpus: Corpus, sequential: int, writers: int, per_writer: int
) -> dict[str, Any]:
    """Sequential ``post_message``, then ``writers`` concurrent writers on the one SQLite writer."""

    def make_post(author: str, index: int):
        text = corpus.text()
        channel = f"bench-{index % CHANNELS}"
        return lambda: hub.post_message(channel, author, text)

    latencies: list[float] = []
    started = time.perf_counter()
    for index in range(sequential):
        ms, _ = await timed(make_post("bench-poster", index))
        latencies.append(ms)
    seq = summarize(latencies, time.perf_counter() - started)

    concurrent_latencies: list[float] = []
    calls = [[make_post(f"bench-writer-{w}", i) for i in range(per_writer)] for w in range(writers)]

    async def writer(batch):
        for call in batch:
            ms, _ = await timed(call)
            concurrent_latencies.append(ms)

    started = time.perf_counter()
    await asyncio.gather(*(writer(batch) for batch in calls))
    concurrent = summarize(concurrent_latencies, time.perf_counter() - started)
    concurrent["writers"] = writers
    return {"sequential": seq, "concurrent": concurrent}


async def scenario_search(hub: Hub, corpus: Corpus, mode: str, queries: int, warmup: int) -> dict[str, Any]:
    """``queries`` searches in one mode: the first call alone (cold), then warm-up, then measured."""
    cold_ms, _ = await timed(lambda: hub.search(corpus.query(), mode=mode))
    for _ in range(warmup):
        await hub.search(corpus.query(), mode=mode)
    latencies: list[float] = []
    hits = 0
    started = time.perf_counter()
    for _ in range(queries):
        text = corpus.query()
        ms, result = await timed(lambda text=text: hub.search(text, mode=mode))
        latencies.append(ms)
        hits += len(result["results"])
    warm = summarize(latencies, time.perf_counter() - started)
    return {
        "mode": mode,
        "cold_first_ms": round(cold_ms, 3),
        "warmup_queries": warmup,
        "avg_hits": round(hits / queries, 2) if queries else 0.0,
        "warm": warm,
    }


async def wait_until(predicate: Callable[[], bool], timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.005)
    return True


async def scenario_waiters(hub: Hub, count: int) -> dict[str, Any]:
    """``count`` parked ``wait_for_mentions`` calls; two posts wake the right halves, nobody is lost."""
    names = [f"bench-waiter-{index:03d}" for index in range(count)]

    async def newest(conn):
        cursor = await conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages")
        return int((await cursor.fetchone())[0])

    since_id = await hub.db.read(newest)
    base_count = hub.waiters.waiter_count
    done_at: dict[str, float] = {}
    results: dict[str, dict[str, Any]] = {}

    async def park(name: str) -> None:
        results[name] = await hub.wait_for_mentions(name, since_id, timeout=120)
        done_at[name] = time.perf_counter()

    tasks = [asyncio.create_task(park(name)) for name in names]
    parked = await wait_until(lambda: hub.waiters.waiter_count >= base_count + count)
    first, second = names[::2], names[1::2]
    latencies: list[float] = []
    wrongly_woken = 0
    wake_wall = 0.0  # sum of post -> last wake over both batches, excluding the settle sleeps

    for group, other in ((first, second), (second, [])):
        text = " ".join(f"@{name}" for name in group) + " bench wake"
        posted = time.perf_counter()
        await hub.post_message("bench-0", "bench-poster", text)
        await wait_until(lambda group=group: all(name in done_at for name in group), timeout=30.0)
        latencies.extend((done_at[name] - posted) * 1000.0 for name in group if name in done_at)
        wake_wall += max((done_at[name] for name in group if name in done_at), default=posted) - posted
        # The other half must still be parked after this post.
        await asyncio.sleep(0.05)
        wrongly_woken += sum(1 for name in other if name in done_at)

    await asyncio.wait(tasks, timeout=5.0)
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    woken = sum(
        1
        for name in names
        if name in results and not results[name]["timed_out"] and results[name]["messages"]
    )
    summary = summarize(latencies, wake_wall)
    return {
        "waiters": count,
        "all_parked": parked,
        "woken": woken,
        "lost": count - woken,
        "wrongly_woken": wrongly_woken,
        "wake": summary,
        "clean": parked and woken == count and wrongly_woken == 0,
    }


async def scenario_feed(hub: Hub, corpus: Corpus, depth: int, flat: int, reads: int) -> dict[str, Any]:
    """Seed a ``depth``-deep reply chain plus ``flat`` top-level posts; read timeline pages and threads."""
    started = time.perf_counter()
    parent = None
    first_id = 0
    for index in range(depth):
        body = f"#bench chain {index} {corpus.text()}"
        post = (await hub.feed_post("bench-feeder", body, parent_id=parent))["post"]
        parent = post["id"]
        first_id = first_id or post["id"]
    deepest = parent
    for _ in range(flat):
        await hub.feed_post(f"bench-feeder-{corpus.rng.randrange(5)}", corpus.text())
    seed_s = time.perf_counter() - started

    async def measure(call: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
        out: list[float] = []
        began = time.perf_counter()
        for _ in range(reads):
            ms, _ = await timed(call)
            out.append(ms)
        return summarize(out, time.perf_counter() - began)

    timeline = await measure(lambda: hub.feed_timeline(scope="global", limit=20))
    thread_deep = await measure(lambda: hub.feed_timeline(scope="thread", post_id=deepest))
    thread_root = await measure(lambda: hub.feed_timeline(scope="thread", post_id=first_id))
    return {
        "thread_depth": depth,
        "flat_posts": flat,
        "seed_s": round(seed_s, 3),
        "timeline": timeline,
        "thread_deep": thread_deep,
        "thread_root": thread_root,
    }


# ---- run ---------------------------------------------------------------------


def environment() -> dict[str, Any]:
    return {
        "cpu_count": os.cpu_count() or 1,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "os": platform.system(),
    }


def presets(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve defaults: explicit flags win over the preset."""
    quick = args.quick
    params = {
        "messages": 2_000 if quick else FULL_MESSAGES,
        "waiters": 50,
        "queries": 30 if quick else 200,
        "seed": args.seed,
        "quick": quick,
        "dim": args.dim,
        "post_sequential": 100 if quick else 1_000,
        "writers": 8,
        "post_per_writer": 15 if quick else 125,
        "warmup": 3 if quick else 20,
        "feed_depth": 100 if quick else 1_000,
        "feed_flat": 100 if quick else 2_000,
        "feed_reads": 20 if quick else 100,
    }
    for key in ("messages", "waiters", "queries"):
        value = getattr(args, key)
        if value is not None:
            params[key] = value
    # Hybrid scans every vector in Python, so a full run may want fewer of them.
    params["hybrid_queries"] = args.hybrid_queries or params["queries"]
    params["warmup"] = min(params["warmup"], params["queries"])
    return params


def evaluate(report: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = report["scenarios"]
    measured = {
        "post_sequential": scenarios["post"]["sequential"]["throughput_per_s"],
        "search_bm25_p95": scenarios["search"]["bm25"]["warm"]["p95_ms"],
        "search_hybrid_p95": scenarios["search"]["hybrid"]["warm"]["p95_ms"],
        "waiters_wake_p95": scenarios["waiters"]["wake"]["p95_ms"],
    }
    full = report["params"]["messages"] >= FULL_MESSAGES
    rows = []
    for name, (unit, op, limit) in TARGETS.items():
        value = measured[name]
        met = value >= limit if op == ">=" else value < limit
        if name == "waiters_wake_p95":
            waiters = scenarios["waiters"]
            met = bool(
                met
                and waiters["waiters"] >= WAITER_TARGET_COUNT
                and waiters["clean"]
            )
        rows.append(
            {
                "name": name,
                "target": f"{op} {limit:g} {unit}",
                "measured": value,
                "unit": unit,
                "met": bool(met),
                "at_target_scale": full,
            }
        )
    return rows


async def run(params: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(params["seed"])
    corpus = Corpus(random.Random(rng.random()))
    embedder = FakeEmbedder(params["dim"])
    with tempfile.TemporaryDirectory(prefix="cat-fleet-bench-", ignore_cleanup_errors=True) as tmp:
        db = Database(str(Path(tmp) / "bench.sqlite"))
        await db.open()
        try:
            if not db.fts5_available:
                raise SystemExit("this SQLite build has no FTS5; the search benchmark cannot run")
            hub = Hub(db, WaitHub())
            hub.embedder = embedder
            scenarios: dict[str, Any] = {}
            progress(f"seeding {params['messages']} messages")
            scenarios["seed"] = await scenario_seed(hub, corpus, embedder, params["messages"], rng)
            progress("post throughput")
            scenarios["post"] = await scenario_post(
                hub, corpus, params["post_sequential"], params["writers"], params["post_per_writer"]
            )
            search: dict[str, Any] = {}
            for mode in ("bm25", "hybrid"):
                progress(f"search {mode}")
                count = params["queries"] if mode == "bm25" else params["hybrid_queries"]
                search[mode] = await scenario_search(
                    hub, corpus, mode, count, min(params["warmup"], count)
                )
            scenarios["search"] = search
            progress("waiters")
            scenarios["waiters"] = await scenario_waiters(hub, params["waiters"])
            progress("feed")
            scenarios["feed"] = await scenario_feed(
                hub, corpus, params["feed_depth"], params["feed_flat"], params["feed_reads"]
            )
        finally:
            await db.close()
    report = {"environment": environment(), "params": params, "scenarios": scenarios}
    report["targets"] = evaluate(report)
    return report


def progress(message: str) -> None:
    print(f"[bench] {message}", file=sys.stderr, flush=True)


def render(report: dict[str, Any]) -> str:
    env, sc = report["environment"], report["scenarios"]
    lines = [
        f"cpus={env['cpu_count']} python={env['python']} sqlite={env['sqlite']} os={env['os']}",
        f"messages={report['params']['messages']} seed={report['params']['seed']}"
        f" quick={report['params']['quick']}",
        "",
        f"{'scenario':<28}{'count':>8}{'wall s':>9}{'per s':>10}{'p50 ms':>10}{'p95 ms':>10}{'p99 ms':>10}",
    ]

    def row(label: str, block: dict[str, Any]) -> None:
        lines.append(
            f"{label:<28}{block['count']:>8}{block['wall_s']:>9.2f}{block['throughput_per_s']:>10.1f}"
            f"{block['p50_ms']:>10.2f}{block['p95_ms']:>10.2f}{block['p99_ms']:>10.2f}"
        )

    seed = sc["seed"]
    lines.append(
        f"{'seed (insert+fts+vectors)':<28}{seed['messages']:>8}{seed['wall_s']:>9.2f}"
        f"{seed['throughput_per_s']:>10.1f}{'-':>10}{'-':>10}{'-':>10}"
    )
    row("post sequential", sc["post"]["sequential"])
    row(f"post concurrent x{sc['post']['concurrent']['writers']}", sc["post"]["concurrent"])
    for mode in ("bm25", "hybrid"):
        row(f"search {mode} (warm)", sc["search"][mode]["warm"])
    row(f"waiters wake x{sc['waiters']['waiters']}", sc["waiters"]["wake"])
    row("feed timeline page", sc["feed"]["timeline"])
    row("feed thread (deepest)", sc["feed"]["thread_deep"])
    row("feed thread (root)", sc["feed"]["thread_root"])
    lines.append("")
    for mode in ("bm25", "hybrid"):
        lines.append(f"search {mode}: first (cold) call {sc['search'][mode]['cold_first_ms']:.2f} ms")
    waiters = sc["waiters"]
    lines.append(
        f"waiters: woken {waiters['woken']}/{waiters['waiters']} lost={waiters['lost']}"
        f" wrongly_woken={waiters['wrongly_woken']}"
    )
    lines.append("")
    scale = "" if report["params"]["messages"] >= FULL_MESSAGES else " (targets are for 100k messages)"
    lines.append(f"targets{scale}:")
    for target in report["targets"]:
        lines.append(
            f"  {'met ' if target['met'] else 'MISS'} {target['name']:<20}"
            f"{target['target']:<16} measured {target['measured']:g} {target['unit']}"
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark the Cat Fleet Chat hub in process.")
    parser.add_argument("--messages", type=int, help=f"seeded messages (default {FULL_MESSAGES}, quick 2000)")
    parser.add_argument("--waiters", type=int, help="concurrent long-poll waiters (default 50)")
    parser.add_argument("--queries", type=int, help="measured queries per search mode (default 200, quick 30)")
    parser.add_argument("--hybrid-queries", type=int, help="measured hybrid queries (default: --queries)")
    parser.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
    parser.add_argument("--dim", type=int, default=EMBED_DIM, help=f"fake embedding width (default {EMBED_DIM})")
    parser.add_argument("--json", metavar="PATH", help="also write the report as JSON")
    parser.add_argument("--quick", action="store_true", help="small preset that finishes in seconds")
    args = parser.parse_args(argv)
    for name in ("messages", "waiters", "queries", "hybrid_queries"):
        value = getattr(args, name)
        if value is not None and value < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.dim < 2:
        parser.error("--dim must be at least 2")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run(presets(args)))
    print(render(report))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
