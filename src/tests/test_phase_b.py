"""
test_phase_b.py — Tests verifying all Phase B correctness fixes.

B1: Agent slug identity (non-empty slug 422, conflict with different name 409, idempotent upsert).
B2: Logging configure_logging (default INFO for serve, emits event=http_request to stderr).
B3: claude -p subprocess passes stdin=subprocess.DEVNULL.
B4: BOARD_API_KEY gates ONLY POST /agents, /posts, /posts/sync; follows/views/reads stay open; hmac.compare_digest.
B5: Demo exit code validation (returns 1 on mismatch).
B6: Batch item failures (BatchResult gets failed_count, PostQueue accumulates, /metrics post_items_failed_total).
B7: LIKE fallback escapes %, _, \\ and aligns parameters when FTS fails with following=True.
B8: /posts/sync returns 200 for duplicate dedupe_key, 201 for new post.
B9: UI contains <link rel="icon" href="data:,">.
"""
from __future__ import annotations

import io
import logging
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from board import config
from board.api import create_app
from board.config import configure_logging
from board.demo import run_demo
from board.llm import LLMResponse, complete_with_metadata
from board.models import AgentRegister, FeedQuery, PostCreate
from board.queue import PostQueue
from board.store.sqlite import SQLiteStore
from swarm.runner import SwarmResult


def test_b1_agent_slug_identity():
    """B1: Empty slug returns 422; duplicate slug with different name returns 409; same name upserts."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    # 1. Punctuation-only produces empty slug -> 422
    r_empty = client.post("/agents", json={"name": "!!!", "function": "Invalid agent name"})
    assert r_empty.status_code == 422
    assert "empty slug" in r_empty.text.lower()

    # 2. Register first agent "C++ Agent" -> slug "c-agent"
    r1 = client.post("/agents", json={"name": "C++ Agent", "function": "C++ specialist"})
    assert r1.status_code == 201
    assert r1.json()["slug"] == "c-agent"

    # 3. Register "C Agent" -> produces same slug "c-agent" with DIFFERENT name -> 409 Conflict
    r2 = client.post("/agents", json={"name": "C Agent", "function": "C specialist"})
    assert r2.status_code == 409
    assert "already exists with a different name" in r2.text

    # 4. Idempotent upsert: same name "C++ Agent" with updated function -> 201 (success)
    r3 = client.post("/agents", json={"name": "C++ Agent", "function": "Updated C++ specialist"})
    assert r3.status_code == 201
    assert r3.json()["function"] == "Updated C++ specialist"


def test_b2_logging_configure_logging(capsys):
    """B2: configure_logging writes key=value lines to stderr with level from LOG_LEVEL."""
    configure_logging(default_level="INFO")

    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    # Perform one request
    client.get("/healthz")

    captured = capsys.readouterr()
    assert "event=http_request" in captured.err
    assert "method=GET" in captured.err
    assert "status=200" in captured.err


def test_b3_claude_stdin_devnull():
    """B3: Subprocess execution passes stdin=subprocess.DEVNULL."""
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Claude output"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        complete_with_metadata("Test devnull", backend="claude")
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs.get("stdin") == subprocess.DEVNULL


def test_b4_board_api_key_scope():
    """B4: BOARD_API_KEY gates ONLY POST /agents, /posts, /posts/sync; follows/views/reads stay open."""
    test_key = "secure-token-12345"
    with patch.object(config, "BOARD_API_KEY", test_key):
        store = SQLiteStore(":memory:")
        store.init()
        # Seed an agent directly in store for testing
        store.register_agent(AgentRegister(name="Seed Agent", function="Tester"))
        app = create_app(store=store, enable_sync=True)
        client = TestClient(app)

        # 1. Gated endpoints fail without key
        assert client.post("/agents", json={"name": "New Ag", "function": "f"}).status_code == 401
        assert client.post("/posts", json={"agent_slug": "seed-agent", "text": "t"}).status_code == 401
        assert client.post("/posts/sync", json={"agent_slug": "seed-agent", "text": "t"}).status_code == 401

        # 2. Gated endpoints succeed with valid Bearer or X-API-Key
        r_post = client.post(
            "/posts",
            json={"agent_slug": "seed-agent", "text": "Authorized post"},
            headers={"Authorization": f"Bearer {test_key}"},
        )
        assert r_post.status_code == 202

        # 3. Follows, saved views, and reads remain open (viewer identity is advisory)
        assert client.get("/feed").status_code == 200
        assert client.get("/search?q=test").status_code == 200
        assert client.get("/agents").status_code == 200

        follow_r = client.post("/follows", json={"agent_slug": "seed-agent"})
        assert follow_r.status_code == 201

        unfollow_r = client.delete("/follows/seed-agent")
        assert unfollow_r.status_code == 200

        view_r = client.post("/views", json={"name": "Open View Preset", "filters": {}})
        assert view_r.status_code == 201
        view_id = view_r.json()["id"]

        del_view_r = client.delete(f"/views/{view_id}")
        assert del_view_r.status_code == 200


def test_b5_demo_failure_exit_validation():
    """B5: run_demo() exits with code 1 if pass 1 has failures or pass 2 produces new posts."""
    # Scenario A: Pass 1 has a failed post
    res1_failed = SwarmResult()
    res1_failed.posts_submitted = 20
    res1_failed.posts_failed = 1
    res2_ok = SwarmResult()
    res2_ok.posts_submitted = 0
    res2_ok.posts_skipped = 20

    with patch("board.demo.run_swarm", side_effect=[res1_failed, res2_ok]):
        exit_code = run_demo()
        assert exit_code == 1

    # Scenario B: Pass 2 produces non-zero new posts
    res1_ok = SwarmResult()
    res1_ok.posts_submitted = 20
    res1_ok.posts_failed = 0
    res2_failed = SwarmResult()
    res2_failed.posts_submitted = 1
    res2_failed.posts_skipped = 19

    with patch("board.demo.run_swarm", side_effect=[res1_ok, res2_failed]):
        exit_code = run_demo()
        assert exit_code == 1


def test_b6_batch_item_failures():
    """B6: Batch item failures are accounted in BatchResult and accumulated in post_items_failed_total."""
    store = SQLiteStore(":memory:")
    store.init()
    store.register_agent(AgentRegister(name="Valid Agent", function="OK"))

    app = create_app(store=store)
    client = TestClient(app)

    items = [
        PostCreate(agent_slug="valid-agent", text="Valid 1"),
        PostCreate(agent_slug="non-existent-agent", text="Bad foreign key item"),
        PostCreate(agent_slug="valid-agent", text="Valid 2"),
    ]

    # Test direct batch execution
    res = store.create_posts_batch(items)
    assert res.inserted_count == 2
    assert res.failed_count == 1
    assert res.failed == 1

    # Test through PostQueue
    queue = app.state.queue
    for it in items:
        queue.put(it)
    queue.flush()

    assert queue.total_failed_items == 1
    assert queue.total_written == 2

    # Verify metric in /metrics
    m_resp = client.get("/metrics")
    assert m_resp.status_code == 200
    assert "post_items_failed_total 1" in m_resp.text


def test_b7_like_fallback_escaping_and_fts_failure_recovery():
    """B7: LIKE fallback escapes %, _, \\ and aligns conditions/params on FTS failure with following=True."""
    store = SQLiteStore(":memory:")
    store.init()
    store._fts_available = False  # Force LIKE fallback

    store.register_agent(AgentRegister(name="Search Agent", function="Testing"))
    store.create_post(PostCreate(agent_slug="search-agent", text="Progress: 100% completed."))
    store.create_post(PostCreate(agent_slug="search-agent", text="Progress: completed without symbol."))
    store.create_post(PostCreate(agent_slug="search-agent", text="Progress: with_underscore_var here."))
    store.create_post(PostCreate(agent_slug="search-agent", text="Progress: with\\backslash path."))

    # 1. Query '%' must match only the post containing a literal percent sign
    p_pct = store.search("%")
    assert len(p_pct) == 1
    assert "100%" in p_pct[0].text

    # Feed query with q='%'
    feed_pct = store.get_feed(FeedQuery(q="%"))
    assert len(feed_pct.posts) == 1
    assert "100%" in feed_pct.posts[0].text

    # 2. Query '_' must match only literal underscore
    p_und = store.search("_")
    assert len(p_und) == 1
    assert "with_underscore_var" in p_und[0].text

    # 3. Query '\\' must match only literal backslash
    p_bs = store.search("\\")
    assert len(p_bs) == 1
    assert "with\\backslash" in p_bs[0].text

    # 4. FTS fallback with following=True and multiple followed agents
    store._fts_available = True
    store.register_agent(AgentRegister(name="Agent One", function="1"))
    store.register_agent(AgentRegister(name="Agent Two", function="2"))
    store.add_follow("viewer1", MagicMock(agent_slug="agent-one"))
    store.add_follow("viewer1", MagicMock(agent_slug="agent-two"))

    store.create_post(PostCreate(agent_slug="agent-one", text="Target match in agent one"))
    store.create_post(PostCreate(agent_slug="agent-two", text="Target match in agent two"))

    # Force OperationalError on MATCH query by dropping posts_fts while _fts_available remains True
    store._conn.execute("DROP TABLE posts_fts")

    feed_fb = store.get_feed(FeedQuery(q="Target", following=True, viewer="viewer1"))
    assert len(feed_fb.posts) == 2
    assert {p.agent_slug for p in feed_fb.posts} == {"agent-one", "agent-two"}


def test_b8_posts_sync_duplicate_dedupe_key():
    """B8: /posts/sync returns 201 for initial insert and 200 for duplicate dedupe_key."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)
    client = TestClient(app)

    client.post("/agents", json={"name": "Sync Tester", "function": "Sync testing"})

    payload = {
        "agent_slug": "sync-tester",
        "text": "Synchronous idempotency test post",
        "dedupe_key": "unique-sync-key-001",
    }

    # Initial synchronous post -> 201 Created
    r1 = client.post("/posts/sync", json=payload)
    assert r1.status_code == 201
    post1_id = r1.json()["id"]

    # Re-post with identical dedupe_key -> 200 OK returning existing post
    r2 = client.post("/posts/sync", json=payload)
    assert r2.status_code == 200
    assert r2.json()["id"] == post1_id


def test_b9_ui_favicon_link():
    """B9: index.html contains data: URI favicon link to prevent /favicon.ico 404s."""
    ui_html = (config.UI_DIR / "index.html").read_text(encoding="utf-8")
    assert '<link rel="icon" href="data:,' in ui_html
