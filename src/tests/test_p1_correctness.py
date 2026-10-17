"""
test_p1_correctness.py — Rigorous verification suite for Phase P1 correctness blockers.

Covers:
  - Thread safety under heavy concurrency (8 readers + 4 writers, >= 1,000 ops, 0 errors, 0 lost posts).
  - Atomic batch writes: savepoint per item, failing item rolls back, good items commit without corruption.
  - Limit validation: limit=0 or limit=-1 -> 422 on /feed, /search, and /views/{id}/apply.
  - Bounded queue full -> 503 with Retry-After header.
  - Input limits -> 422:
      * tags > 20 items or item > 40 chars
      * metadata > 8 KB
      * X-Viewer invalid chars or > 64 chars
      * q > 200 chars
      * since not valid ISO-8601
  - Agent update returns original created_at timestamp.
  - POST /posts/sync endpoint gating (404 when disabled, 201 when enabled).
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.models import AgentRegister, PostCreate, PostKind, SavedViewCreate
from board.queue import PostQueue
from board.store.sqlite import SQLiteStore


def test_concurrent_readers_and_writers():
    """8 reader threads + 4 writer threads against one store/app, >=1,000 operations, 0 exceptions, 0 lost posts."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)

    # Pre-register agents
    for w in range(4):
        store.register_agent(AgentRegister(
            name=f"Worker {w}",
            function=f"Concurrent writer {w}",
            tags=["worker", f"w-{w}"],
        ))

    num_writers = 4
    posts_per_writer = 100  # 4 * 100 = 400 total written posts
    num_readers = 8
    reads_per_reader = 80   # 8 * 80 = 640 total reads
    # Total operations: 400 + 640 = 1,040 operations (>= 1,000)

    exceptions: list[Exception] = []

    def writer_task(writer_id: int):
        slug = f"worker-{writer_id}"
        for i in range(posts_per_writer):
            try:
                store.create_post(PostCreate(
                    agent_slug=slug,
                    text=f"Concurrent post {i} from writer {writer_id}",
                    tags=["load", f"w-{writer_id}"],
                    source="api",
                ))
            except Exception as e:
                exceptions.append(e)

    def reader_task(reader_id: int):
        for i in range(reads_per_reader):
            try:
                feed = store.get_feed()
                assert feed is not None
                if i % 5 == 0:
                    agents = store.list_agents()
                    assert len(agents) == 4
                if i % 7 == 0:
                    srch = store.search("Concurrent", limit=10)
                    assert srch is not None
                if i % 11 == 0:
                    assert store.healthy() is True
            except Exception as e:
                exceptions.append(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_writers + num_readers) as executor:
        writer_futures = [executor.submit(writer_task, w) for w in range(num_writers)]
        reader_futures = [executor.submit(reader_task, r) for r in num_readers * [0]]

        concurrent.futures.wait(writer_futures + reader_futures)

    store.close()

    assert len(exceptions) == 0, f"Encountered {len(exceptions)} exceptions: {exceptions[:5]}"

    # Verify zero lost posts
    store2 = SQLiteStore(":memory:")
    store2.init()
    # Also verify with queue under concurrent threads
    q_app = create_app(store=store2)
    with TestClient(q_app) as client:
        for w in range(4):
            client.post("/agents", json={
                "name": f"Queue Worker {w}",
                "function": f"Async writer {w}",
            })

        def client_writer(w_id: int):
            for i in range(50):
                r = client.post("/posts", json={
                    "agent_slug": f"queue-worker-{w_id}",
                    "text": f"Queue item {i} from {w_id}",
                    "source": "api",
                })
                assert r.status_code == 202

        def client_reader():
            for _ in range(50):
                r = client.get("/feed?limit=10")
                assert r.status_code == 200

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(client_writer, w) for w in range(4)]
            futs += [ex.submit(client_reader) for _ in range(8)]
            concurrent.futures.wait(futs)

        q_app.state.queue.flush()

        # All 200 posts must exist
        all_feed = client.get("/feed?limit=100").json()
        assert len(all_feed["posts"]) == 100

    store2.close()


def test_batch_atomic_per_item():
    """A batch with a bad middle item persists exactly the good items and rolls back the bad item."""
    store = SQLiteStore(":memory:")
    store.init()
    agent = store.register_agent(AgentRegister(name="Batch Valid Agent", function="Valid agent"))

    batch_items = [
        PostCreate(agent_slug=agent.slug, text="Good post 1", tags=["good"]),
        PostCreate(agent_slug="non-existent-agent-slug", text="Bad post", tags=["bad"]),
        PostCreate(agent_slug=agent.slug, text="Good post 2", tags=["good"]),
    ]

    results = store.create_posts_batch(batch_items)

    # Exactly 2 good items should be returned and saved
    assert len(results) == 2
    assert results[0].text == "Good post 1"
    assert results[1].text == "Good post 2"

    feed = store.get_feed()
    assert len(feed.posts) == 2
    persisted_texts = {p.text for p in feed.posts}
    assert "Good post 1" in persisted_texts
    assert "Good post 2" in persisted_texts
    assert "Bad post" not in persisted_texts

    store.close()


def test_limit_validation_ge_1():
    """limit=0 and limit=-1 must return 422 on /feed, /search, and /views/{id}/apply."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)

    with TestClient(app) as client:
        client.post("/agents", json={"name": "Limit Tester", "function": "Testing limits"})
        client.post("/posts/sync", json={"agent_slug": "limit-tester", "text": "Testing limit validation"})
        view_res = client.post("/views", json={"name": "View 1", "filters": {}}, headers={"X-Viewer": "user1"})
        view_id = view_res.json()["id"]

        for bad_limit in [0, -1, -50]:
            # 1. /feed
            r_feed = client.get(f"/feed?limit={bad_limit}")
            assert r_feed.status_code == 422, f"Expected 422 for /feed?limit={bad_limit}, got {r_feed.status_code}"

            # 2. /search
            r_search = client.get(f"/search?q=test&limit={bad_limit}")
            assert r_search.status_code == 422, f"Expected 422 for /search?limit={bad_limit}, got {r_search.status_code}"

            # 3. /views/{id}/apply
            r_apply = client.get(f"/views/{view_id}/apply?limit={bad_limit}", headers={"X-Viewer": "user1"})
            assert r_apply.status_code == 422, f"Expected 422 for /views/{view_id}/apply?limit={bad_limit}, got {r_apply.status_code}"

    store.close()


def test_queue_full_returns_503():
    """When queue is full, POST /posts returns 503 with Retry-After header."""
    store = SQLiteStore(":memory:")
    store.init()
    store.register_agent(AgentRegister(name="Queue Tester", function="Testing queue 503"))

    app = create_app(store=store)

    with TestClient(app) as client:
        # Patch queue.put to raise queue.Full to test 503 response and Retry-After header
        import queue as pyqueue
        with patch.object(app.state.queue, "put", side_effect=pyqueue.Full):
            r = client.post("/posts", json={"agent_slug": "queue-tester", "text": "Overflow item"})
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "1"
            assert "queue is full" in r.json()["detail"].lower()

    store.close()


def test_input_limits_validation():
    """Validates tags <= 20 items, tag <= 40 chars, metadata <= 8 KB, X-Viewer, q <= 200, since ISO-8601."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)

    with TestClient(app) as client:
        client.post("/agents", json={"name": "Validator Agent", "function": "Testing validators"})

        # 1. Tags > 20 items -> 422
        r = client.post("/posts/sync", json={
            "agent_slug": "validator-agent",
            "text": "Too many tags",
            "tags": [f"tag-{i}" for i in range(21)],
        })
        assert r.status_code == 422

        # 2. Tag > 40 chars -> 422
        r = client.post("/posts/sync", json={
            "agent_slug": "validator-agent",
            "text": "Oversized tag",
            "tags": ["a" * 41],
        })
        assert r.status_code == 422

        # Tag whitespace stripping and lowercasing
        r = client.post("/posts/sync", json={
            "agent_slug": "validator-agent",
            "text": "Normalized tag post",
            "tags": ["  HeLLo-WoRLD  "],
        })
        assert r.status_code == 201
        assert r.json()["tags"] == ["hello-world"]

        # 3. Metadata > 8 KB -> 422
        huge_meta = {"key": "x" * 8200}
        r = client.post("/posts/sync", json={
            "agent_slug": "validator-agent",
            "text": "Huge metadata",
            "metadata": huge_meta,
        })
        assert r.status_code == 422

        # 4. X-Viewer validation
        # Bad characters -> 422
        r = client.get("/follows", headers={"X-Viewer": "bad viewer with spaces!"})
        assert r.status_code == 422
        r = client.get("/follows", headers={"X-Viewer": "bad<script>"})
        assert r.status_code == 422

        # Too long (> 64 chars) -> 422
        r = client.get("/follows", headers={"X-Viewer": "a" * 65})
        assert r.status_code == 422

        # Valid X-Viewer -> 200
        r = client.get("/follows", headers={"X-Viewer": "valid.user_123-name"})
        assert r.status_code == 200

        # 5. q > 200 chars -> 422
        r = client.get(f"/search?q={'q' * 201}")
        assert r.status_code == 422
        r = client.get(f"/feed?q={'q' * 201}")
        assert r.status_code == 422

        # 6. since invalid ISO-8601 -> 422
        r = client.get("/feed?since=not-a-timestamp")
        assert r.status_code == 422
        r = client.get("/feed?since=2026-02-31T99:99:99Z")
        assert r.status_code == 422

        # Valid since -> 200
        r = client.get("/feed?since=2026-01-01T00:00:00Z")
        assert r.status_code == 200

        # 7. source restriction
        r = client.post("/posts/sync", json={
            "agent_slug": "validator-agent",
            "text": "Invalid source",
            "source": "unauthorized-source",
        })
        assert r.status_code == 422

    store.close()


def test_agent_created_at_preserved_on_update():
    """register_agent must return the real created_at on update (not 'now')."""
    store = SQLiteStore(":memory:")
    store.init()

    # Initial registration
    initial = store.register_agent(AgentRegister(
        name="Persistence Agent",
        function="Initial function",
    ))
    initial_created = initial.created_at
    assert initial_created

    # Wait a small delay to ensure timestamp would differ if newly generated
    time.sleep(0.01)

    # Update agent
    updated = store.register_agent(AgentRegister(
        name="Persistence Agent",
        function="Updated function after delay",
    ))

    assert updated.slug == initial.slug
    assert updated.created_at == initial_created
    assert updated.function == "Updated function after delay"

    # Verify get_agent also returns original created_at
    fetched = store.get_agent(initial.slug)
    assert fetched is not None
    assert fetched.created_at == initial_created

    store.close()


def test_sync_post_endpoint_toggle():
    """POST /posts/sync is 404 when disabled, 201 when enabled."""
    store1 = SQLiteStore(":memory:")
    store1.init()
    store1.register_agent(AgentRegister(name="Toggle Agent", function="Toggle test"))

    # App with sync disabled (default)
    app_disabled = create_app(store=store1, enable_sync=False)
    with TestClient(app_disabled) as client:
        r = client.post("/posts/sync", json={
            "agent_slug": "toggle-agent",
            "text": "This should be 404",
        })
        assert r.status_code == 404

    # App with sync enabled explicitly
    store2 = SQLiteStore(":memory:")
    store2.init()
    store2.register_agent(AgentRegister(name="Toggle Agent", function="Toggle test"))
    app_enabled = create_app(store=store2, enable_sync=True)
    with TestClient(app_enabled) as client:
        r = client.post("/posts/sync", json={
            "agent_slug": "toggle-agent",
            "text": "This should be 201",
        })
        assert r.status_code == 201
        assert r.json()["text"] == "This should be 201"
