# Benchmark

`bench.py` drives `Hub` and `Database` in process on a temporary SQLite file.
No network, no real embedder, synthetic data only. The temp directory is
removed on exit and the default data directory is never touched. Runs are
deterministic for a given `--seed` (timings, of course, are not).

```bash
uv run --extra dev python bench/bench.py                 # full run: 100k messages
uv run --extra dev python bench/bench.py --quick         # ~2k messages, seconds
uv run --extra dev python bench/bench.py --json out.json # also write the report
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--messages N` | 100000 (`--quick`: 2000) | seeded messages |
| `--waiters N` | 50 | concurrent `wait_for_mentions` long-polls |
| `--queries N` | 200 (`--quick`: 30) | measured queries per search mode |
| `--hybrid-queries N` | `--queries` | measured hybrid queries (each scans every vector in Python, so a full run may want fewer) |
| `--seed N` | 7 | random seed |
| `--dim N` | 384 | width of the fake embedder's vectors |
| `--json PATH` | off | write the report as JSON |
| `--quick` | off | small preset, used by `tests/test_bench.py` |

## Scenarios

- **seed**: one bulk transaction of messages and mentions across 6 channels
  and 24 agents (Zipf-like synthetic vocabulary), then the FTS5 index via the
  project's own backfill SQL, then one vector per message. Seed time is
  reported by phase and is not part of any other number.
- **post**: sequential `hub.post_message`, then 8 concurrent writers. The
  concurrent run shows the single-writer ceiling: all writers queue on one
  SQLite connection, so throughput stays flat while per-post latency grows.
- **search**: `hub.search` for `mode=bm25`, and `mode=hybrid` through a
  deterministic hash-based fake embedder (bag-of-words buckets, no model),
  installed through the hub's public `embedder` attribute. Queries are 1 to 3
  words from the vocabulary, so common words (many matches) are included.
  The first call is reported alone as cold; warm-up queries run next; then
  the measured warm queries.
- **waiters**: 50 parked `wait_for_mentions` calls. One post mentions half of
  them and must wake exactly those; a second post wakes the rest. Reports
  wake latency (post call start to the waiter's return), how many woke, how
  many were lost and how many woke when they should not have.
- **feed**: a 1000-post reply chain (`feed.DEPTH_MAX`) plus 2000 flat posts;
  global timeline pages and thread reads at the deepest post and the root.

The script prints CPU count, Python, SQLite and OS family. It never prints
hostnames or paths.

## Targets

Judged on a full run (100k messages) on a developer machine.

| Target | Goal |
| --- | --- |
| `post_sequential` | at least 500 posts/s |
| `search_bm25_p95` | below 50 ms at 100k messages |
| `search_hybrid_p95` | below 250 ms at 100k messages (fake embedder, 384 dims) |
| `waiters_wake_p95` | 50 waiters wake with p95 below 200 ms, none lost |

## Results

Machine: 24 logical CPUs, Windows, Python 3.14.7, SQLite 3.53.1. One
full run: `python bench/bench.py --queries 200 --hybrid-queries 20`
(hybrid took 20 queries because each takes about 5 s).

| Scenario | Count | Wall s | Per s | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| seed (insert + FTS + vectors) | 100000 | 12.88 | 7765 | - | - | - |
| post sequential | 1000 | 3.71 | 269.7 | 2.91 | 4.13 | 29.39 |
| post concurrent x8 | 1000 | 3.35 | 298.9 | 23.75 | 51.54 | 65.79 |
| search bm25 (warm) | 200 | 4.90 | 40.8 | 9.44 | 111.54 | 115.48 |
| search hybrid (warm) | 20 | 81.36 | 0.2 | 3973.22 | 4409.80 | 6569.97 |
| waiters wake x50 | 50 | 0.19 | 267.1 | 52.26 | 100.25 | 108.02 |
| feed timeline page | 100 | 0.24 | 417.3 | 2.36 | 2.81 | 3.01 |
| feed thread (deepest) | 100 | 4.76 | 21.0 | 52.73 | 57.42 | 61.42 |
| feed thread (root) | 100 | 0.41 | 241.9 | 4.08 | 4.67 | 4.80 |

Cold first call: bm25 36 ms, hybrid 3619 ms. Waiters: 50 of 50 woke, none lost,
none woken by the wrong post.

| Target | Measured | Met |
| --- | --- | --- |
| post sequential >= 500/s | 269.7/s | no |
| search bm25 p95 < 50 ms | 111.5 ms | no |
| search hybrid p95 < 250 ms | 4410 ms | no |
| 50 waiters wake p95 < 200 ms | 100.3 ms | yes |

Reading the misses:

- **Posts** commit one transaction each. 270/s is a Windows file system
  number; run the bench on the machine that will host the hub before drawing
  a conclusion. Concurrent writers do not add throughput (299/s), which is the
  single-writer ceiling.
- **BM25** p50 is 9 ms. The p95 comes from queries made of one or two very
  common words: FTS5 has to rank every matching row (tens of thousands) before
  it can return the top 20.
- **Hybrid** is a full scan: `Hub._vector_rank` unpacks and dot-multiplies
  every stored vector in pure Python, so cost is linear in the number of
  vectors (about 45 µs each at 384 dims). It is fine at a few thousand
  messages (about 90 ms at 2k) and far over target at 100k.
- **Waiters** meet the target. Wake time grows with waiter count because every
  parked waiter re-queries after each notification.
