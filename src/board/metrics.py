"""
board.metrics — Zero-dependency Prometheus metrics collector.

Tracks:
  - posts_accepted_total: counter
  - posts_written_total: counter
  - posts_duplicate_total: counter
  - post_batches_failed_total: counter
  - queue_depth: gauge
  - http_requests_total: counter (labels: method, handler, status)
  - http_request_duration_seconds: histogram (labels: method, handler)

Thread-safe and exposes metrics in Prometheus 0.0.4 text format.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from board.queue import PostQueue

HISTOGRAM_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class MetricsCollector:
    """Zero-dependency Prometheus-compatible metrics registry and collector."""

    def __init__(self, queue_getter: Callable[[], PostQueue | None] | None = None) -> None:
        self._lock = threading.Lock()
        self._queue_getter = queue_getter
        self._posts_accepted = 0
        self._sync_posts_written = 0
        self._precheck_duplicates = 0
        self._http_requests: dict[tuple[str, str, str], int] = {}
        self._http_durations: dict[tuple[str, str], list[float]] = {}

    def inc_posts_accepted(self, count: int = 1) -> None:
        with self._lock:
            self._posts_accepted += count

    def inc_sync_written(self, count: int = 1) -> None:
        with self._lock:
            self._sync_posts_written += count

    def inc_precheck_duplicates(self, count: int = 1) -> None:
        with self._lock:
            self._precheck_duplicates += count

    def record_http_request(
        self, method: str, handler: str, status: int, duration_seconds: float
    ) -> None:
        key = (method.upper(), handler, str(status))
        h_key = (method.upper(), handler)
        with self._lock:
            self._http_requests[key] = self._http_requests.get(key, 0) + 1
            if h_key not in self._http_durations:
                self._http_durations[h_key] = []
            self._http_durations[h_key].append(duration_seconds)

    def generate_prometheus_text(self) -> str:
        queue = self._queue_getter() if self._queue_getter else None
        q_written = queue.total_written if queue else 0
        q_duplicates = queue.total_duplicates if queue else 0
        q_failed = queue.total_failed_batches if queue else 0
        q_depth = queue.pending if queue else 0

        with self._lock:
            accepted = self._posts_accepted
            written = q_written + self._sync_posts_written
            duplicates = q_duplicates + self._precheck_duplicates
            reqs = dict(self._http_requests)
            durations = {k: list(v) for k, v in self._http_durations.items()}

        lines: list[str] = []

        # posts_accepted_total
        lines.append(
            "# HELP posts_accepted_total Total posts accepted into the write queue or written synchronously."
        )
        lines.append("# TYPE posts_accepted_total counter")
        lines.append(f"posts_accepted_total {accepted}")

        # posts_written_total
        lines.append("# HELP posts_written_total Total posts committed to SQLite store.")
        lines.append("# TYPE posts_written_total counter")
        lines.append(f"posts_written_total {written}")

        # posts_duplicate_total
        lines.append("# HELP posts_duplicate_total Total duplicate posts detected or skipped.")
        lines.append("# TYPE posts_duplicate_total counter")
        lines.append(f"posts_duplicate_total {duplicates}")

        # post_batches_failed_total
        lines.append("# HELP post_batches_failed_total Total batch transactions that failed.")
        lines.append("# TYPE post_batches_failed_total counter")
        lines.append(f"post_batches_failed_total {q_failed}")

        # queue_depth
        lines.append("# HELP queue_depth Current number of posts pending in the write queue.")
        lines.append("# TYPE queue_depth gauge")
        lines.append(f"queue_depth {q_depth}")

        # http_requests_total
        lines.append("# HELP http_requests_total Total HTTP requests handled.")
        lines.append("# TYPE http_requests_total counter")
        for (m, h, s), count in sorted(reqs.items()):
            lines.append(f'http_requests_total{{method="{m}",handler="{h}",status="{s}"}} {count}')

        # http_request_duration_seconds
        lines.append("# HELP http_request_duration_seconds HTTP request latency in seconds.")
        lines.append("# TYPE http_request_duration_seconds histogram")
        for (m, h), samples in sorted(durations.items()):
            total_sum = sum(samples)
            total_count = len(samples)
            for le in HISTOGRAM_BUCKETS:
                cumulative = sum(1 for x in samples if x <= le)
                lines.append(
                    f'http_request_duration_seconds_bucket{{method="{m}",handler="{h}",le="{le}"}} {cumulative}'
                )
            lines.append(
                f'http_request_duration_seconds_bucket{{method="{m}",handler="{h}",le="+Inf"}} {total_count}'
            )
            lines.append(
                f'http_request_duration_seconds_sum{{method="{m}",handler="{h}"}} {total_sum:.6f}'
            )
            lines.append(
                f'http_request_duration_seconds_count{{method="{m}",handler="{h}"}} {total_count}'
            )

        return "\n".join(lines) + "\n"
