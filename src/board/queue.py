"""
board.queue — Bounded in-process PostQueue with batching writer.

Architecture:
  API handler -> queue.put(post) -> [background thread] -> store.create_posts_batch()

The background thread drains the queue in batches, writing to the store in
single transactions. This amortizes SQLite write overhead and avoids
contention on the API hot path.

Thread-safe. Graceful shutdown via close().
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

from board import config
from board.models import PostCreate, Post

if TYPE_CHECKING:
    from board.store.base import Store

logger = logging.getLogger(__name__)


class PostQueue:
    """Bounded queue with a background batch-writer thread."""

    def __init__(
        self,
        store: Store,
        max_size: int = 1000,
        batch_size: int = 50,
        batch_interval_ms: int = 200,
    ):
        self._store = store
        self._queue: queue.Queue[PostCreate] = queue.Queue(maxsize=max_size)
        self._batch_size = batch_size
        self._batch_interval = batch_interval_ms / 1000.0
        self._stop = threading.Event()
        self._stats_lock = threading.Lock()
        self._total_written = 0
        self._total_duplicates = 0
        self._total_failed_batches = 0
        self._thread = threading.Thread(
            target=self._writer_loop, name="post-queue-writer", daemon=True,
        )
        self._thread.start()

    def put(self, post: PostCreate, timeout: float | None = None) -> None:
        """Enqueue a post for batch writing. Raises queue.Full if full after timeout."""
        t = timeout if timeout is not None else config.POST_QUEUE_PUT_TIMEOUT
        self._queue.put(post, block=True, timeout=t)

    def flush(self) -> None:
        """Block until the queue is fully drained."""
        self._queue.join()

    def close(self) -> None:
        """Signal the writer to stop and wait for it to finish."""
        self._stop.set()
        self._thread.join(timeout=5)

    @property
    def pending(self) -> int:
        """Number of posts waiting to be written."""
        return self._queue.qsize()

    @property
    def total_written(self) -> int:
        with self._stats_lock:
            return self._total_written

    @property
    def total_duplicates(self) -> int:
        with self._stats_lock:
            return self._total_duplicates

    @property
    def total_failed_batches(self) -> int:
        with self._stats_lock:
            return self._total_failed_batches

    def _writer_loop(self) -> None:
        """Background thread: drain queue in batches."""
        while not self._stop.is_set():
            batch: list[PostCreate] = []
            deadline = time.monotonic() + self._batch_interval

            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=min(remaining, 0.1))
                    batch.append(item)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue

            if batch:
                try:
                    res = self._store.create_posts_batch(batch)
                    with self._stats_lock:
                        self._total_written += getattr(res, "inserted_count", len(res))
                        self._total_duplicates += getattr(res, "skipped_count", 0)
                except Exception:
                    with self._stats_lock:
                        self._total_failed_batches += 1
                    logger.exception("Failed to write batch of %d posts", len(batch))
                finally:
                    for _ in batch:
                        self._queue.task_done()

        # Drain remaining items on shutdown
        remaining: list[PostCreate] = []
        while not self._queue.empty():
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            try:
                res = self._store.create_posts_batch(remaining)
                with self._stats_lock:
                    self._total_written += getattr(res, "inserted_count", len(res))
                    self._total_duplicates += getattr(res, "skipped_count", 0)
            except Exception:
                with self._stats_lock:
                    self._total_failed_batches += 1
                logger.exception("Failed to write final batch of %d posts", len(remaining))
            finally:
                for _ in remaining:
                    self._queue.task_done()
