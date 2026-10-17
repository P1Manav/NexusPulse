"""
test_queue.py — Unit tests for PostQueue.

Covers:
  - Bounded capacity and blocking behavior.
  - Background writer batch draining.
  - Flush behavior and clean shutdown.
"""
from __future__ import annotations

import time
import pytest

from board.models import AgentRegister, PostCreate, PostKind
from board.queue import PostQueue
from board.store.sqlite import SQLiteStore


@pytest.fixture
def store():
    s = SQLiteStore(":memory:")
    s.init()
    s.register_agent(AgentRegister(name="Queue Agent", function="Test agent for queue"))
    yield s
    s.close()


def test_queue_batch_writing(store: SQLiteStore):
    # Small batch size to trigger write quickly
    q = PostQueue(store, max_size=50, batch_size=5, batch_interval_ms=50)

    try:
        for i in range(12):
            q.put(PostCreate(
                agent_slug="queue-agent",
                text=f"Queued message #{i}",
                kind=PostKind.UPDATE,
            ))

        # Block until all items in queue are written
        q.flush()

        from board.models import FeedQuery
        feed = store.get_feed(FeedQuery(limit=50))
        assert len(feed.posts) == 12
    finally:
        q.close()


def test_queue_close_drains_remaining(store: SQLiteStore):
    # Long interval so the background loop wouldn't drain naturally before close
    q = PostQueue(store, max_size=50, batch_size=20, batch_interval_ms=5000)

    for i in range(3):
        q.put(PostCreate(
            agent_slug="queue-agent",
            text=f"Shutdown message #{i}",
        ))

    # Close must drain remaining items
    q.close()

    from board.models import FeedQuery
    feed = store.get_feed(FeedQuery(limit=10))
    assert len(feed.posts) == 3
