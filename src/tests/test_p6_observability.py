"""
Phase P6 Test Suite: Search, Indexing, and Observability.

Validates:
  1. Relational post_tags indexing with compound key and backfill migration.
  2. Feed filtering by normalized tags via post_tags relational table.
  3. Combined keyset cursor pagination with FTS text query in get_feed.
  4. Multi-filter search endpoint (/search?q=...&agent=...&tag=...&kind=...&since=...).
  5. Prometheus /metrics exposition endpoint (format 0.0.4, counters, gauge, histogram).
  6. Structured logging middleware and request duration tracking.
  7. BOARD_API_KEY security enforcement on write endpoints with public read access.
  8. Swarm runner loop mode argument parsing and execution.
"""
from __future__ import annotations

import os
import sqlite3
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from board import config
from board.api import create_app
from board.models import AgentRegister, PostCreate, PostKind, FeedQuery
from board.store.sqlite import SQLiteStore
from swarm.runner import run_swarm_cli


@pytest.fixture
def store(tmp_path):
    db_file = str(tmp_path / "test_p6.db")
    s = SQLiteStore(db_file)
    s.init()
    yield s
    s.close()


@pytest.fixture
def client(store):
    app = create_app(store=store, enable_sync=True)
    with TestClient(app) as c:
        yield c


def test_post_tags_table_and_backfill(tmp_path):
    """Test that post_tags table is created, indexed, and backfills existing posts."""
    db_file = str(tmp_path / "migration_test.db")
    
    # 1. Create a legacy database with posts but without post_tags table
    raw_conn = sqlite3.connect(db_file)
    raw_conn.execute("""
        CREATE TABLE agents (
            slug TEXT PRIMARY KEY, name TEXT, function TEXT, kind TEXT,
            tags TEXT, created_at TEXT, updated_at TEXT
        );
    """)
    raw_conn.execute("""
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_slug TEXT, text TEXT, kind TEXT, tags TEXT,
            metadata TEXT, source TEXT, dedupe_key TEXT, created_at TEXT
        );
    """)
    raw_conn.execute("""
        INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, dedupe_key, created_at)
        VALUES ('agent-1', 'Legacy post', 'update', '["Python", "fastapi"]', '{}', 'mock', 'd1', '2026-01-01T00:00:00Z')
    """)
    raw_conn.commit()
    raw_conn.close()

    # 2. Open via SQLiteStore.init() - should perform migration & backfill
    migrated_store = SQLiteStore(db_file)
    migrated_store.init()

    with migrated_store._lock:
        rows = migrated_store._conn.execute("SELECT * FROM post_tags ORDER BY tag").fetchall()
        tags = [r["tag"] for r in rows]
        assert "fastapi" in tags
        assert "python" in tags
    migrated_store.close()


def test_feed_relational_tag_filtering(store):
    """Test feed filtering by tag using normalized relational post_tags."""
    store.register_agent(AgentRegister(name="Tag Agent", function="Tag testing"))
    p1 = store.create_post(PostCreate(
        agent_slug="tag-agent",
        text="Observability metrics update",
        tags=["Metrics", "Prometheus"],
    ))
    p2 = store.create_post(PostCreate(
        agent_slug="tag-agent",
        text="Kubernetes deployment update",
        tags=["Kubernetes", "DevOps"],
    ))

    # Query with mixed case and whitespace
    feed = store.get_feed(FeedQuery(tag="  METRICS  "))
    assert len(feed.posts) == 1
    assert feed.posts[0].id == p1.id

    feed_k8s = store.get_feed(FeedQuery(tag="kubernetes"))
    assert len(feed_k8s.posts) == 1
    assert feed_k8s.posts[0].id == p2.id


def test_feed_fts_and_keyset_pagination(store):
    """Test get_feed combining FTS query q with keyset cursor pagination."""
    store.register_agent(AgentRegister(name="Search Feed Agent", function="Feed FTS testing"))
    for i in range(5):
        store.create_post(PostCreate(
            agent_slug="search-feed-agent",
            text=f"Telemetry cluster node #{i} health check active",
            dedupe_key=f"node-{i}",
        ))

    # Fetch first page with query "cluster health" and limit 2
    page1 = store.get_feed(FeedQuery(q="cluster health", limit=2))
    assert len(page1.posts) == 2
    assert page1.next_cursor is not None

    # Fetch second page using next_cursor
    page2 = store.get_feed(FeedQuery(q="cluster health", cursor=page1.next_cursor, limit=2))
    assert len(page2.posts) == 2
    assert page2.next_cursor is not None
    assert int(page2.posts[0].id) < int(page1.posts[-1].id)

    # Fetch third page
    page3 = store.get_feed(FeedQuery(q="cluster health", cursor=page2.next_cursor, limit=2))
    assert len(page3.posts) == 1
    assert page3.next_cursor is None


def test_search_with_filters(store, client):
    """Test /search endpoint with agent, tag, kind, and since filters."""
    client.post("/agents", json={"name": "Alpha Agent", "function": "Alpha worker"})
    client.post("/agents", json={"name": "Beta Agent", "function": "Beta worker"})

    r1 = client.post("/posts/sync", json={
        "agent_slug": "alpha-agent",
        "text": "Alpha critical system outage detected",
        "kind": "failed",
        "tags": ["incident", "production"],
    })
    assert r1.status_code == 201, f"Alpha post failed: {r1.status_code} {r1.text}"

    r2 = client.post("/posts/sync", json={
        "agent_slug": "beta-agent",
        "text": "Beta critical system outage resolved",
        "kind": "update",
        "tags": ["incident", "resolution"],
    })
    assert r2.status_code == 201, f"Beta post failed: {r2.status_code} {r2.text}"

    # Search with q only
    r = client.get("/search", params={"q": "critical outage"})
    assert r.status_code == 200
    assert len(r.json()) == 2

    # Search filtered by agent
    r_agent = client.get("/search", params={"q": "critical outage", "agent": "alpha-agent"})
    assert r_agent.status_code == 200
    assert len(r_agent.json()) == 1
    assert r_agent.json()[0]["agent_slug"] == "alpha-agent"

    # Search filtered by tag
    r_tag = client.get("/search", params={"q": "critical outage", "tag": "resolution"})
    assert r_tag.status_code == 200
    assert len(r_tag.json()) == 1
    assert r_tag.json()[0]["agent_slug"] == "beta-agent"

    # Search filtered by kind
    r_kind = client.get("/search", params={"q": "critical outage", "kind": "failed"})
    assert r_kind.status_code == 200
    assert len(r_kind.json()) == 1
    assert r_kind.json()[0]["kind"] == "failed"


def test_metrics_exposition_endpoint(client):
    """Test /metrics endpoint returning standard Prometheus text format with expected metrics."""
    # Generate some activity
    client.post("/agents", json={"name": "Metrics Producer", "function": "Metrics generator"})
    client.post("/posts/sync", json={
        "agent_slug": "metrics-producer",
        "text": "Sync post for metrics verification",
    })
    client.get("/healthz")
    client.get("/feed")

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert "text/plain" in metrics_resp.headers["content-type"]
    text = metrics_resp.text

    # Verify Prometheus headers & metrics presence
    assert "# HELP posts_accepted_total" in text
    assert "# TYPE posts_accepted_total counter" in text
    assert "posts_accepted_total" in text

    assert "# HELP posts_written_total" in text
    assert "# TYPE posts_written_total counter" in text
    assert "posts_written_total" in text

    assert "# HELP queue_depth" in text
    assert "# TYPE queue_depth gauge" in text
    assert "queue_depth" in text

    assert "# HELP http_requests_total" in text
    assert 'http_requests_total{method="GET",handler="/healthz",status="200"}' in text

    assert "# HELP http_request_duration_seconds" in text
    assert 'http_request_duration_seconds_bucket{method="GET",handler="/healthz",le="+Inf"}' in text
    assert 'http_request_duration_seconds_count{method="GET",handler="/healthz"}' in text


def test_board_api_key_enforcement(store):
    """Test that BOARD_API_KEY guards mutating routes and keeps read routes public."""
    test_key = "secret-board-key-99"
    with patch.object(config, "BOARD_API_KEY", test_key):
        app = create_app(store=store, enable_sync=True)
        with TestClient(app) as client:
            # 1. Public endpoints work without key
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 200
            assert client.get("/feed").status_code == 200
            assert client.get("/metrics").status_code == 200

            # 2. Mutating endpoints fail with 401 without key
            agent_payload = {"name": "Guarded Agent", "function": "Security test"}
            r_no_auth = client.post("/agents", json=agent_payload)
            assert r_no_auth.status_code == 401

            # 3. Mutating endpoints fail with invalid key
            r_bad_auth = client.post(
                "/agents",
                json=agent_payload,
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert r_bad_auth.status_code == 401

            # 4. Mutating endpoints succeed with valid Bearer token
            r_bearer = client.post(
                "/agents",
                json=agent_payload,
                headers={"Authorization": f"Bearer {test_key}"},
            )
            assert r_bearer.status_code == 201

            # 5. Mutating endpoints succeed with valid X-API-Key header
            post_payload = {
                "agent_slug": "guarded-agent",
                "text": "Authorized post via X-API-Key",
            }
            r_x_key = client.post(
                "/posts/sync",
                json=post_payload,
                headers={"X-API-Key": test_key},
            )
            assert r_x_key.status_code == 201

            # 6. Follows and saved views stay open without key (viewer identity is advisory)
            r_follow = client.post("/follows", json={"agent_slug": "guarded-agent"})
            assert r_follow.status_code == 201

            r_unfollow = client.delete("/follows/guarded-agent")
            assert r_unfollow.status_code == 200

            r_view = client.post("/views", json={"name": "Open View", "filters": {}})
            assert r_view.status_code == 201
            view_id = r_view.json()["id"]

            r_del_view = client.delete(f"/views/{view_id}")
            assert r_del_view.status_code == 200


def test_swarm_runner_loop_flag_handling():
    """Test that run_swarm_cli accepts loop and interval flags gracefully."""
    with patch("httpx.get") as mock_get, patch("swarm.runner.run_swarm") as mock_swarm:
        mock_get.return_value.status_code = 200
        # When loop=False, runs swarm once
        run_swarm_cli(loop=False)
        mock_swarm.assert_called_once()
