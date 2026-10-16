"""The benchmark script runs end to end on its quick preset and reports sane numbers."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "bench" / "bench.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("fleet_bench", BENCH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet_bench"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("fleet_bench", None)


@pytest.fixture(scope="module")
def report(bench, tmp_path_factory):
    out = tmp_path_factory.mktemp("bench") / "report.json"
    assert bench.main(["--quick", "--json", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _latency(block):
    assert block["count"] > 0
    assert block["p50_ms"] > 0
    assert block["p95_ms"] >= block["p50_ms"]
    assert block["p99_ms"] >= block["p95_ms"]


def test_report_sections(report):
    assert {"environment", "params", "scenarios", "targets"} <= set(report)
    assert set(report["scenarios"]) == {"seed", "post", "search", "waiters", "feed"}
    env = report["environment"]
    assert env["cpu_count"] >= 1
    assert env["python"] and env["sqlite"]


def test_seed_and_post(report):
    seed = report["scenarios"]["seed"]
    assert seed["messages"] == report["params"]["messages"]
    assert seed["throughput_per_s"] > 0
    for name in ("sequential", "concurrent"):
        block = report["scenarios"]["post"][name]
        assert block["count"] > 0
        assert block["throughput_per_s"] > 0
        _latency(block)
    assert report["scenarios"]["post"]["concurrent"]["writers"] > 1


def test_search_modes(report):
    search = report["scenarios"]["search"]
    for mode in ("bm25", "hybrid"):
        block = search[mode]
        assert block["avg_hits"] > 0
        assert block["cold_first_ms"] > 0
        _latency(block["warm"])
        assert block["warm"]["throughput_per_s"] > 0


def test_waiters_none_lost(report):
    waiters = report["scenarios"]["waiters"]
    assert waiters["waiters"] == report["params"]["waiters"]
    assert waiters["woken"] == waiters["waiters"]
    assert waiters["lost"] == 0
    assert waiters["wrongly_woken"] == 0
    _latency(waiters["wake"])


def test_feed(report):
    feed = report["scenarios"]["feed"]
    assert feed["thread_depth"] > 1
    _latency(feed["timeline"])
    _latency(feed["thread_deep"])


def test_targets_listed(report):
    names = {row["name"] for row in report["targets"]}
    assert {"post_sequential", "search_bm25_p95", "search_hybrid_p95", "waiters_wake_p95"} <= names
    for row in report["targets"]:
        assert isinstance(row["met"], bool)
        assert row["measured"] > 0


def test_report_has_no_paths_or_hostnames(report):
    text = json.dumps(report)
    assert "\\" not in text and "/tmp" not in text and "Users" not in text
