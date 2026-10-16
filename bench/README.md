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
| seed (insert + FTS + vectors) | 100000 | 16.96 | 5896 | - | - | - |
| post sequential | 1000 | 5.01 | 199.6 | 4.19 | 5.18 | 33.20 |
| post concurrent x8 | 1000 | 5.16 | 193.9 | 35.00 | 68.75 | 81.69 |
| search bm25 (warm) | 200 | 6.10 | 32.8 | 11.09 | 135.92 | 151.94 |
| search hybrid (warm) | 20 | 93.66 | 0.2 | 4592.40 | 5006.79 | 6544.68 |
| waiters wake x50 | 50 | 0.16 | 310.8 | 69.83 | 149.38 | 160.88 |
| feed timeline page | 100 | 0.34 | 293.9 | 3.37 | 4.00 | 4.16 |
| feed thread (deepest) | 100 | 4.30 | 23.2 | 43.12 | 47.78 | 60.47 |
| feed thread (root) | 100 | 0.59 | 168.6 | 5.82 | 6.99 | 7.56 |

Cold first call: bm25 46 ms, hybrid 4772 ms. Waiters: 50 of 50 woke, none lost,
none woken by the wrong post.

| Target | Measured | Met |
| --- | --- | --- |
| post sequential >= 500/s | 199.6/s | no |
| search bm25 p95 < 50 ms | 135.9 ms | no |
| search hybrid p95 < 250 ms | 5007 ms | no |
| 50 waiters wake p95 < 200 ms | 149.4 ms | yes |

Reading the misses:

- **Posts** commit one transaction each. 200/s is a Windows file system
  number; run the bench on the machine that will host the hub before drawing
  a conclusion. Concurrent writers do not add throughput (194/s), which is the
  single-writer ceiling.
- **BM25** p50 is 11 ms. The p95 comes from queries made of one or two very
  common words: FTS5 has to rank every matching row (tens of thousands) before
  it can return the top 20.
- **Hybrid** is a full scan: `Hub._vector_rank` unpacks and dot-multiplies
  every stored vector in pure Python, so cost is linear in the number of
  vectors (about 45 µs each at 384 dims). It is fine at a few thousand
  messages (about 90 ms at 2k) and far over target at 100k.
- **Waiters** meet the target. Wake time grows with waiter count because every
  parked waiter re-queries after each notification.
