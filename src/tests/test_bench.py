"""
test_bench.py — Verification of benchmarking computation functions.
"""
from __future__ import annotations

from board.bench import compute_metrics, ScenarioResult


def test_compute_metrics_calculations():
    latencies = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    duration = 1.0  # 1 second
    res = compute_metrics("Test Scenario", latencies, duration)

    assert isinstance(res, ScenarioResult)
    assert res.name == "Test Scenario"
    assert res.total_ops == 10
    assert res.duration_s == 1.0
    assert res.ops_per_sec == 10.0
    assert res.mean_ms == 55.0
    assert 50.0 <= res.p50_ms <= 60.0
    assert res.p95_ms > res.p50_ms
    assert res.p99_ms >= res.p95_ms


def test_compute_metrics_empty():
    res = compute_metrics("Empty Scenario", [], 0.0)
    assert res.total_ops == 0
    assert res.ops_per_sec == 0.0
