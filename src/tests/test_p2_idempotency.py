"""
test_p2_idempotency.py — Verification suite for Phase P2 swarm idempotency and deduplication.

Covers:
  - Running swarm twice -> post count unchanged, second pass produces 0 new and 20 skipped.
  - Running swarm with force=True -> post count doubles.
  - Concurrent identical posts with same dedupe_key -> race-safe single insertion.
  - Direct API POST /posts with same dedupe_key twice -> returns 200 {"status": "duplicate"}, exactly one row.
  - GET /posts/exists returns correct boolean status.
  - FTS5 virtual table contains no duplicate entries for deduplicated posts.
  - WorkItem explicit key overrides default hash.
"""
from __future__ import annotations

import concurrent.futures
import threading
import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.models import AgentKind, AgentRegister, PostCreate, PostKind, FeedQuery
from board.store.sqlite import SQLiteStore
from swarm.loader import AgentSpec, WorkItem
from swarm.runner import run_swarm, compute_dedupe_key


def test_swarm_twice_idempotency():
    """Running the swarm twice against an API results in unchanged post count and 20 skipped on second run."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    with TestClient(app) as client:
        base_url = "http://testserver"

        # Pass 1: Fresh run
        res1 = run_swarm(base_url, client=client)
        assert res1.posts_submitted == 20
        assert res1.posts_skipped == 0

        # Flush queue
        app.state.queue.flush()

        feed1 = store.get_feed(FeedQuery(limit=100))
        initial_count = len(feed1.posts)
        assert initial_count == 20

        # Pass 2: Re-run without force
        res2 = run_swarm(base_url, force=False, client=client)
        assert res2.posts_submitted == 0
        assert res2.posts_skipped == 20

        app.state.queue.flush()

        feed2 = store.get_feed(FeedQuery(limit=100))
        assert len(feed2.posts) == initial_count  # Post count completely unchanged!

    store.close()


def test_swarm_force_flag_doubles_count():
    """Running swarm with force=True forces re-posting and doubles the post count."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    with TestClient(app) as client:
        base_url = "http://testserver"

        # Pass 1
        res1 = run_swarm(base_url, client=client)
        assert res1.posts_submitted == 20
        app.state.queue.flush()

        # Pass 2 with force=True
        res2 = run_swarm(base_url, force=True, client=client)
        assert res2.posts_submitted == 20
        assert res2.posts_skipped == 0
        app.state.queue.flush()

        feed = store.get_feed(FeedQuery(limit=100))
        assert len(feed.posts) == 40  # Exactly doubled!

    store.close()


def test_concurrent_identical_posts_race_safety():
    """Multiple concurrent threads attempting to insert the exact same dedupe_key yields exactly 1 row and 1 FTS entry."""
    store = SQLiteStore(":memory:")
    store.init()
    agent = store.register_agent(AgentRegister(name="Race Agent", function="Concurrency testing"))

    dedupe_key = "race-test-dedupe-key-12345"
    num_threads = 10
    barrier = threading.Barrier(num_threads)
    created_posts = []
    lock = threading.Lock()

    def try_insert():
        barrier.wait()
        p = store.create_post(PostCreate(
            agent_slug=agent.slug,
            text="Race condition test post",
            dedupe_key=dedupe_key,
        ))
        with lock:
            created_posts.append(p)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as ex:
        futures = [ex.submit(try_insert) for _ in range(num_threads)]
        concurrent.futures.wait(futures)

    # Database posts count must be exactly 1
    feed = store.get_feed(FeedQuery(limit=100))
    matching_posts = [p for p in feed.posts if p.dedupe_key == dedupe_key]
    assert len(matching_posts) == 1

    # FTS search must also have exactly 1 match
    search_res = store.search("Race condition test post")
    assert len(search_res) == 1

    store.close()


def test_api_direct_dedupe_post_and_exists_endpoint():
    """POST /posts with duplicate dedupe_key returns 200 {"status": "duplicate"}, and GET /posts/exists reports presence."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    with TestClient(app) as client:
        client.post("/agents", json={"name": "API Dedupe Agent", "function": "Testing dedupe"})

        # Check existence before posting
        r_pre = client.get("/posts/exists?agent_slug=api-dedupe-agent&dedupe_key=my-key-001")
        assert r_pre.status_code == 200
        assert r_pre.json()["exists"] is False

        # First post -> 202 accepted
        r1 = client.post("/posts", json={
            "agent_slug": "api-dedupe-agent",
            "text": "First insertion",
            "dedupe_key": "my-key-001",
        })
        assert r1.status_code == 202
        assert r1.json()["status"] == "accepted"

        # Drain queue
        app.state.queue.flush()

        # Check existence after posting
        r_post = client.get("/posts/exists?agent_slug=api-dedupe-agent&dedupe_key=my-key-001")
        assert r_post.status_code == 200
        assert r_post.json()["exists"] is True

        # Second post with identical dedupe_key -> 200 duplicate
        r2 = client.post("/posts", json={
            "agent_slug": "api-dedupe-agent",
            "text": "Second attempt with same key",
            "dedupe_key": "my-key-001",
        })
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"
        assert r2.json()["dedupe_key"] == "my-key-001"

        app.state.queue.flush()

        # In-batch duplicate resolution test:
        # Two identical calls sent before flush both return 202; unique index stores 1 row; 3rd call returns 200
        batch_key = "in-batch-dedupe-key-999"
        b1 = client.post("/posts", json={
            "agent_slug": "api-dedupe-agent",
            "text": "First batch attempt",
            "dedupe_key": batch_key,
        })
        b2 = client.post("/posts", json={
            "agent_slug": "api-dedupe-agent",
            "text": "Second batch attempt identical key",
            "dedupe_key": batch_key,
        })
        assert b1.status_code == 202
        assert b1.json()["status"] == "accepted"
        assert b2.status_code == 202
        assert b2.json()["status"] == "accepted"

        # Drain queue to SQLite
        app.state.queue.flush()

        feed_batch = client.get("/feed?agent=api-dedupe-agent").json()
        matching = [p for p in feed_batch["posts"] if p["dedupe_key"] == batch_key]
        assert len(matching) == 1

        # Third call after flush returns 200 with status duplicate
        b3 = client.post("/posts", json={
            "agent_slug": "api-dedupe-agent",
            "text": "Third attempt post-flush",
            "dedupe_key": batch_key,
        })
        assert b3.status_code == 200
        assert b3.json()["status"] == "duplicate"
        assert b3.json()["dedupe_key"] == batch_key

    store.close()


def test_work_item_explicit_key_override():
    """Explicit work item key overrides standard sha256 generation."""
    item_with_key = WorkItem(title="Task A", task="Do work", key="custom-explicit-key-xyz")
    assert compute_dedupe_key("agent-slug", item_with_key) == "custom-explicit-key-xyz"

    item_without_key = WorkItem(title="Task A", task="Do work")
    computed = compute_dedupe_key("agent-slug", item_without_key)
    assert len(computed) == 32
    assert computed != "custom-explicit-key-xyz"
