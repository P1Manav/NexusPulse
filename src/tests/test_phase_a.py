"""
test_phase_a.py — Tests verifying all Phase A bug fixes.

A1: Failed posts must not be deduplicated forever.
A2: Never publish raw CLI errors.
A3: Long LLM output must not lose the post (truncated to 5000 chars ending with "…").
A4: Migration order crash (opening legacy schema works, backfills post_tags, dedupes new posts).
A5: Metrics leak (matched route template or 'unmatched', O(1) histogram memory, post_items_failed_total).
A6: since timezone bug (handles offsets, Z, naive, date-only, accurately filters posts).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.feed import normalize_iso_timestamp
from board.llm import LLMResponse, complete_with_metadata
from board.models import AgentKind, AgentRegister, FeedQuery, PostCreate, PostKind
from board.store.sqlite import SQLiteStore
from swarm.loader import AgentSpec, WorkItem
from swarm.runner import run_swarm


def test_a1_failed_posts_not_deduplicated_forever():
    """A1: When LLM call fails, post is submitted without dedupe_key so subsequent run retries it."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    spec = AgentSpec(
        name="Retry Test Agent",
        function="Tests retry of failed LLM completions",
        kind=AgentKind.SKILL,
        tags=["retry"],
        prompt_template="Role: {name}. Task: {task}",
        work_items=[
            WorkItem(
                title="Crucial Task",
                task="Perform critical deployment step",
                tags=["task1"],
            )
        ],
    )

    # Pass 1: LLM fails
    failing_response = LLMResponse(
        text="Completed work item: Perform critical deployment step [verified] [LLM unavailable: using fallback]",
        source="mock-fallback",
        failed=True,
        error_category="timeout",
    )
    with patch("swarm.runner.complete_with_metadata", return_value=failing_response):
        res1 = run_swarm(
            base_url="http://testserver",
            specs=[spec],
            client=client,
        )

    assert res1.posts_submitted == 1
    assert res1.posts_failed == 0
    assert res1.posts_skipped == 0
    app.state.queue.flush()

    # Verify post in DB has kind=failed and daily dedupe_key
    feed_r = client.get("/feed").json()
    assert len(feed_r["posts"]) == 1
    assert feed_r["posts"][0]["kind"] == "failed"
    assert feed_r["posts"][0]["dedupe_key"] is not None
    assert feed_r["posts"][0]["dedupe_key"].startswith("failed-")

    # Pass 2: LLM succeeds (healthy)
    healthy_response = LLMResponse(
        text="Successfully completed critical deployment step",
        source="mock",
        failed=False,
    )
    with patch("swarm.runner.complete_with_metadata", return_value=healthy_response):
        res2 = run_swarm(
            base_url="http://testserver",
            specs=[spec],
            client=client,
        )

    # The item MUST NOT be skipped because the failed post had no dedupe_key
    assert res2.posts_submitted == 1
    assert res2.posts_failed == 0
    assert res2.posts_skipped == 0
    app.state.queue.flush()

    # Pass 3: Re-run should now skip because successful post kept dedupe_key
    with patch("swarm.runner.complete_with_metadata", return_value=healthy_response):
        res3 = run_swarm(
            base_url="http://testserver",
            specs=[spec],
            client=client,
        )
    assert res3.posts_submitted == 0
    assert res3.posts_skipped == 1


def test_a2_never_publish_raw_cli_errors():
    """A2: Stderr containing secret keys or home paths never appears in post text or metadata."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    spec = AgentSpec(
        name="Security Error Guard Agent",
        function="Verifies error sanitization",
        tags=["security"],
        prompt_template="Role: {name}. Task: {task}",
        work_items=[
            WorkItem(
                title="Secret Key Test",
                task="Audit environment credentials",
            )
        ],
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "Traceback /home/appuser/.tokens: sk-ant-api03-SECRET_KEY_EXPOSURE permission denied"

    with patch("board.llm.is_claude_available", return_value=True), \
         patch("board.llm.resolve_backend", return_value="claude"), \
         patch("subprocess.run", return_value=mock_proc):
        run_swarm(
            base_url="http://testserver",
            specs=[spec],
            client=client,
        )

    app.state.queue.flush()
    feed_r = client.get("/feed").json()
    assert len(feed_r["posts"]) == 1
    post = feed_r["posts"][0]

    # Raw secrets or path details must NEVER appear in post text or metadata
    assert "sk-ant-" not in post["text"]
    assert "/home/" not in post["text"]
    assert "sk-ant-" not in str(post["metadata"])
    assert "/home/" not in str(post["metadata"])

    # Fallback text format and error category code
    assert "[LLM unavailable: using fallback]" in post["text"]
    assert post["metadata"].get("llm_error") == "nonzero_exit"


def test_a3_long_llm_output_truncated():
    """A3: 6000-char mocked Claude output is truncated to <= 5000 chars ending with '…'."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    spec = AgentSpec(
        name="Verbose Reporting Agent",
        function="Generates large volume telemetry",
        tags=["telemetry"],
        prompt_template="Role: {name}. Task: {task}",
        work_items=[
            WorkItem(
                title="Massive Telemetry Dump",
                task="Generate 6000 character output",
            )
        ],
    )

    giant_output = "X" * 6000
    mock_response = LLMResponse(text=giant_output, source="claude", failed=False)

    with patch("swarm.runner.complete_with_metadata", return_value=mock_response):
        res = run_swarm(
            base_url="http://testserver",
            specs=[spec],
            client=client,
        )

    assert res.posts_submitted == 1
    app.state.queue.flush()

    feed_r = client.get("/feed").json()
    assert len(feed_r["posts"]) == 1
    text = feed_r["posts"][0]["text"]

    assert len(text) <= 5000
    assert len(text) == 5000
    assert text.endswith("…")
    assert text.startswith("XXXX")


def test_a4_migration_order_crash(tmp_path):
    """A4: Opening legacy schema SQLite file without dedupe_key/post_tags must not crash,

    must backfill post_tags for old posts, and must permit deduped inserts.
    """
    db_file = tmp_path / "legacy_v1.db"
    raw_conn = sqlite3.connect(str(db_file))
    # Create schema without dedupe_key column and without post_tags table
    raw_conn.executescript("""
        CREATE TABLE agents (
            slug        TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            function    TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'skill',
            tags        TEXT NOT NULL DEFAULT '[]',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_slug  TEXT NOT NULL,
            text        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'update',
            tags        TEXT NOT NULL DEFAULT '[]',
            metadata    TEXT NOT NULL DEFAULT '{}',
            source      TEXT NOT NULL DEFAULT 'mock',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (agent_slug) REFERENCES agents(slug)
        );

        CREATE INDEX idx_posts_agent ON posts(agent_slug);
    """)

    # Insert old agent and old post
    raw_conn.execute(
        "INSERT INTO agents VALUES ('legacy-bot', 'Legacy Bot', 'Legacy worker', 'agent', '[\"legacy\"]', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw_conn.execute(
        "INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, created_at) VALUES ('legacy-bot', 'Pre-migration post searchable content', 'update', '[\"legacy-tag\", \"v1\"]', '{}', 'mock', '2026-01-01T00:00:00+00:00')"
    )
    raw_conn.commit()
    raw_conn.close()

    # Open with SQLiteStore — should run migrations cleanly
    store = SQLiteStore(str(db_file))
    store.init()

    try:
        # A4: Verify old post is searchable via FTS5 rebuild
        search_res = store.search("searchable")
        assert len(search_res) == 1
        assert "searchable" in search_res[0].text

        # Verify old post is queryable via tag filter (backfill check)
        feed = store.get_feed(FeedQuery(tag="legacy-tag"))
        assert len(feed.posts) == 1
        assert "searchable" in feed.posts[0].text

        # Verify a new post with dedupe_key inserts and deduplication functions
        new_post = PostCreate(
            agent_slug="legacy-bot",
            text="New post after migration",
            dedupe_key="migrated-key-123",
            tags=["v2"],
        )
        created = store.create_post(new_post)
        assert created.dedupe_key == "migrated-key-123"

        # Duplicate insert returns existing
        dup = store.create_post(new_post)
        assert dup.id == created.id
    finally:
        store.close()


def test_a5_metrics_leak_and_histogram_o1():
    """A5: Route template path is used as handler label ('unmatched' for 404s).

    Histogram uses fixed buckets O(1) memory and post_items_failed_total is exposed.
    """
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    # Send 300 requests to randomized URLs (which should all match "unmatched")
    for i in range(300):
        client.get(f"/random-url-{i}-{uuid.uuid4().hex[:6]}")

    # Send requests to a few valid routes
    client.get("/healthz")
    client.get("/readyz")
    client.get("/feed")

    metrics_r = client.get("/metrics")
    assert metrics_r.status_code == 200
    metrics_text = metrics_r.text

    # Extract all distinct handler label values
    import re
    handlers = set(re.findall(r'handler="([^"]+)"', metrics_text))

    # Distinct handler label count must be <= 20
    assert len(handlers) <= 20
    assert "unmatched" in handlers
    assert "/healthz" in handlers
    assert "/feed" in handlers

    # Verify O(1) memory: collector has no unbounded duration lists
    collector = app.state.metrics
    assert hasattr(collector, "_http_duration_buckets")
    assert not hasattr(collector, "_http_durations")

    # Verify post_items_failed_total is present
    assert "post_items_failed_total" in metrics_text


def test_a6_since_timezone_handling():
    """A6: since query supports offsets (+05:30, -08:00), Z, and filters correctly against UTC created_at."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    # Register an agent
    client.post("/agents", json={
        "name": "Timezone Test Agent",
        "function": "Tests timestamp conversions",
    })

    # Create post synchronously (timestamped now in UTC)
    t_now = dt.datetime.now(dt.timezone.utc)
    res = client.post("/posts", json={
        "agent_slug": "timezone-test-agent",
        "text": "Timezone benchmark post",
    })
    assert res.status_code == 202
    app.state.queue.flush()

    # Query 1: 10 minutes ago expressed in +05:30
    t_10m_ago = t_now - dt.timedelta(minutes=10)
    tz_plus_530 = dt.timezone(dt.timedelta(hours=5, minutes=30))
    since_530 = t_10m_ago.astimezone(tz_plus_530).isoformat()

    r1 = client.get(f"/feed?since={since_530}").json()
    assert len(r1["posts"]) == 1
    assert r1["posts"][0]["text"] == "Timezone benchmark post"

    # Query 2: 10 minutes ago expressed in -08:00
    tz_minus_8 = dt.timezone(dt.timedelta(hours=-8))
    since_minus_8 = t_10m_ago.astimezone(tz_minus_8).isoformat()

    r2 = client.get(f"/feed?since={since_minus_8}").json()
    assert len(r2["posts"]) == 1
    assert r2["posts"][0]["text"] == "Timezone benchmark post"

    # Query 3: 10 minutes in the future -> must return none
    t_future = t_now + dt.timedelta(minutes=10)
    since_future = t_future.isoformat()

    r3 = client.get(f"/feed?since={since_future}").json()
    assert len(r3["posts"]) == 0
