"""
board.bench — Rigorous Load & Latency Benchmark.

Measures:
  1. Batched Post Ingestion Throughput (POST /posts via PostQueue)
  2. Synchronous Write Baseline (POST /posts/sync direct transaction)
  3. Keyset Cursor Feed Read Latency (GET /feed?limit=20)
  4. Ranked Full-Text Search Latency (GET /search?q=... via SQLite FTS5)

Outputs real measured metrics (p50, p95, p99, operations/sec).
Never invents numbers.
"""
from __future__ import annotations

import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import httpx
import uvicorn

from board.api import create_app
from board.models import AgentRegister
from board.store.sqlite import SQLiteStore


class ScenarioResult(NamedTuple):
    name: str
    total_ops: int
    duration_s: float
    ops_per_sec: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def compute_metrics(name: str, latencies_ms: list[float], duration_s: float) -> ScenarioResult:
    """Compute percentiles and throughput from raw millisecond measurements."""
    if not latencies_ms:
        return ScenarioResult(name, 0, duration_s, 0.0, 0.0, 0.0, 0.0, 0.0)

    sorted_lats = sorted(latencies_ms)
    n = len(sorted_lats)

    def percentile(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        d0 = sorted_lats[f] * (c - k)
        d1 = sorted_lats[c] * (k - f)
        return d0 + d1

    return ScenarioResult(
        name=name,
        total_ops=n,
        duration_s=duration_s,
        ops_per_sec=n / duration_s if duration_s > 0 else 0.0,
        mean_ms=statistics.mean(sorted_lats),
        p50_ms=percentile(0.50),
        p95_ms=percentile(0.95),
        p99_ms=percentile(0.99),
    )


def run_benchmarks(base_url: str, app_instance) -> list[ScenarioResult]:
    """Execute all benchmark scenarios against the running API."""
    results: list[ScenarioResult] = []

    # Persistent connection pool client for benchmark
    with httpx.Client(
        base_url=base_url,
        timeout=10.0,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
    ) as client:
        # Setup: Register benchmark test agents
        client.post("/agents", json={
            "name": "Benchmark Producer",
            "function": "Generates synthetic high-frequency telemetry",
            "tags": ["bench", "load-test"],
        })

        # ── Scenario 1: Batched Post Ingestion (POST /posts) ───────────
        post_count = 500
        concurrency = 4

        def send_queued_post(idx: int) -> float:
            t0 = time.perf_counter()
            resp = client.post("/posts", json={
                "agent_slug": "benchmark-producer",
                "text": f"Telemetry metric sample #{idx}: CPU load 42%, cluster healthy",
                "kind": "update",
                "tags": ["bench", "metrics", f"worker-{idx % 4}"],
            })
            resp.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        print(f"[*] Running Scenario 1: Async Batched Post Ingestion ({post_count} posts, {concurrency} threads)...")
        start_time = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            queue_latencies = list(pool.map(send_queued_post, range(post_count)))
        queue_duration = time.perf_counter() - start_time

        # Flush queue to database
        if hasattr(app_instance.state, "queue") and app_instance.state.queue is not None:
            app_instance.state.queue.flush()

        results.append(compute_metrics("1. POST /posts (Queue Batching)", queue_latencies, queue_duration))

        # ── Scenario 2: Synchronous Write Baseline (POST /posts/sync) ──
        sync_count = 100

        def send_sync_post(idx: int) -> float:
            t0 = time.perf_counter()
            resp = client.post("/posts/sync", json={
                "agent_slug": "benchmark-producer",
                "text": f"Direct synchronous write #{idx}",
                "kind": "update",
            })
            resp.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        print(f"[*] Running Scenario 2: Synchronous Write Baseline ({sync_count} posts direct)...")
        start_time = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            sync_latencies = list(pool.map(send_sync_post, range(sync_count)))
        sync_duration = time.perf_counter() - start_time
        results.append(compute_metrics("2. POST /posts/sync (Direct Write)", sync_latencies, sync_duration))

        # ── Scenario 3: Keyset Cursor Feed Read Latency ─────────────────
        read_count = 200

        def read_feed_page(idx: int) -> float:
            t0 = time.perf_counter()
            resp = client.get("/feed?limit=20")
            resp.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        print(f"[*] Running Scenario 3: Keyset Cursor Feed Reads ({read_count} requests)...")
        start_time = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            read_latencies = list(pool.map(read_feed_page, range(read_count)))
        read_duration = time.perf_counter() - start_time
        results.append(compute_metrics("3. GET /feed (Keyset Pagination)", read_latencies, read_duration))

        # ── Scenario 4: FTS5 Full-Text Search Latency ───────────────────
        search_count = 200
        search_queries = ["CPU load", "telemetry", "healthy", "cluster", "worker"]

        def search_query(idx: int) -> float:
            query_word = search_queries[idx % len(search_queries)]
            t0 = time.perf_counter()
            resp = client.get(f"/search?q={query_word}&limit=15")
            resp.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        print(f"[*] Running Scenario 4: FTS5 Full-Text Search ({search_count} queries)...")
        start_time = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            search_latencies = list(pool.map(search_query, range(search_count)))
        search_duration = time.perf_counter() - start_time
        results.append(compute_metrics("4. GET /search (FTS5 BM25 Ranking)", search_latencies, search_duration))

        # ── Scenario 5: Mixed Load (8 Readers + 4 Writers) ────────────
        mixed_ops = 240
        mixed_workers = 12

        def mixed_op(idx: int) -> float:
            t0 = time.perf_counter()
            if idx % 3 == 0:
                resp = client.post("/posts", json={
                    "agent_slug": "benchmark-producer",
                    "text": f"Mixed load stream post #{idx}",
                    "kind": "update",
                    "tags": ["bench", "mixed"],
                })
            elif idx % 3 == 1:
                resp = client.get("/feed?limit=20")
            else:
                resp = client.get("/search?q=telemetry&limit=15")
            resp.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        print(f"[*] Running Scenario 5: Mixed Load ({mixed_ops} ops, 8 readers + 4 writers)...")
        start_time = time.perf_counter()
        with ThreadPoolExecutor(max_workers=mixed_workers) as pool:
            mixed_latencies = list(pool.map(mixed_op, range(mixed_ops)))
        mixed_duration = time.perf_counter() - start_time
        if hasattr(app_instance.state, "queue") and app_instance.state.queue is not None:
            app_instance.state.queue.flush()
        results.append(compute_metrics("5. Mixed Load (8 Readers + 4 Writers)", mixed_latencies, mixed_duration))

        # ── Scenario 6: High Concurrency (16 Concurrent Workers) ──────
        high_ops = 320
        high_workers = 16

        def high_concurrency_op(idx: int) -> float:
            t0 = time.perf_counter()
            resp = client.get("/feed?limit=20")
            resp.raise_for_status()
            return (time.perf_counter() - t0) * 1000.0

        print(f"[*] Running Scenario 6: High Concurrency ({high_ops} requests, 16 workers)...")
        start_time = time.perf_counter()
        with ThreadPoolExecutor(max_workers=high_workers) as pool:
            high_latencies = list(pool.map(high_concurrency_op, range(high_ops)))
        high_duration = time.perf_counter() - start_time
        results.append(compute_metrics("6. GET /feed (16 Concurrent Workers)", high_latencies, high_duration))

    return results


def print_results_table(results: list[ScenarioResult]) -> None:
    """Print clean ASCII table of measured benchmark metrics."""
    print("\n" + "=" * 96)
    print("  MEASURED BENCHMARK RESULTS (REAL LOCAL RUN -- ZERO INVENTED NUMBERS)")
    print("=" * 96)
    header = f"{'Scenario':<38} | {'Ops':>5} | {'Throughput':>12} | {'p50 (ms)':>9} | {'p95 (ms)':>9} | {'p99 (ms)':>9}"
    print(header)
    print("-" * 96)

    for r in results:
        line = (
            f"{r.name:<38} | "
            f"{r.total_ops:>5} | "
            f"{r.ops_per_sec:>9.1f} op/s | "
            f"{r.p50_ms:>8.2f} ms | "
            f"{r.p95_ms:>8.2f} ms | "
            f"{r.p99_ms:>8.2f} ms"
        )
        print(line)

    print("=" * 96)


def run_bench() -> int:
    """CLI handler for `python src/main.py bench`."""
    port = _find_free_port()
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)

    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Wait for server readiness
    for _ in range(50):
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        print("[ERROR] Server failed to start for benchmark")
        return 1

    try:
        results = run_benchmarks(base_url, app)
        print_results_table(results)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        store.close()

    return 0
