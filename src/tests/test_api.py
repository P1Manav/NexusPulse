"""
test_api.py — Integration tests for FastAPI endpoints.

Covers:
  - Agent registration & listing (POST /agents, GET /agents).
  - Posts creation via queue (POST /posts) & synchronous (POST /posts/sync).
  - Feed queries with filtering and keyset pagination (GET /feed).
  - Full-text search (GET /search).
  - Follows and views with X-Viewer headers (POST /follows, GET /follows, etc.).
  - Health checks (/healthz, /readyz).
"""
from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.store.sqlite import SQLiteStore


@pytest.fixture
def client():
    """Provides a TestClient backed by an isolated in-memory store."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)
    with TestClient(app) as test_client:
        yield test_client
    store.close()


def test_health_and_ready(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_agent_registration_and_list(client: TestClient):
    payload = {
        "name": "Cloud Provisioner",
        "function": "Provisions infrastructure via Terraform",
        "kind": "skill",
        "tags": ["cloud", "iac"],
    }
    r = client.post("/agents", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["slug"] == "cloud-provisioner"
    assert data["name"] == "Cloud Provisioner"
    assert data["kind"] == "skill"
    assert data["tags"] == ["cloud", "iac"]

    # List agents
    r_list = client.get("/agents")
    assert r_list.status_code == 200
    agents = r_list.json()
    assert len(agents) == 1
    assert agents[0]["slug"] == "cloud-provisioner"


def test_post_sync_and_feed(client: TestClient):
    # Register agent first
    client.post("/agents", json={
        "name": "Reporter Agent",
        "function": "Generates periodic status reports",
    })

    # Post synchronous
    post_payload = {
        "agent_slug": "reporter-agent",
        "text": "Generated weekly rollup successfully",
        "kind": "update",
        "tags": ["weekly", "metrics"],
        "metadata": {"metrics_count": 42},
        "source": "mock",
    }
    r_post = client.post("/posts/sync", json=post_payload)
    assert r_post.status_code == 201
    post = r_post.json()
    assert post["agent_slug"] == "reporter-agent"
    assert post["id"] is not None

    # Fetch feed
    r_feed = client.get("/feed")
    assert r_feed.status_code == 200
    feed = r_feed.json()
    assert len(feed["posts"]) == 1
    assert feed["posts"][0]["text"] == "Generated weekly rollup successfully"


def test_post_queue_async_batching(client: TestClient):
    client.post("/agents", json={
        "name": "Batch Worker",
        "function": "Processes stream of async updates",
    })

    # Send 5 posts to async queue endpoint
    for i in range(5):
        r = client.post("/posts", json={
            "agent_slug": "batch-worker",
            "text": f"Async stream item {i}",
            "kind": "update",
            "tags": ["async"],
        })
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"

    # Wait for queue drain
    # PostQueue flush
    queue = client.app.state.queue
    queue.flush()

    r_feed = client.get("/feed?agent=batch-worker")
    assert r_feed.status_code == 200
    assert len(r_feed.json()["posts"]) == 5


def test_post_unknown_agent_returns_404(client: TestClient):
    r = client.post("/posts/sync", json={
        "agent_slug": "ghost-agent",
        "text": "This should fail",
        "kind": "update",
    })
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_feed_pagination(client: TestClient):
    client.post("/agents", json={"name": "Feed Paginator", "function": "Testing pagination"})

    for i in range(7):
        client.post("/posts/sync", json={
            "agent_slug": "feed-paginator",
            "text": f"Event #{i}",
            "kind": "update",
        })

    # Page 1: limit 3
    r1 = client.get("/feed?limit=3")
    assert r1.status_code == 200
    p1 = r1.json()
    assert len(p1["posts"]) == 3
    assert p1["next_cursor"] is not None

    # Page 2: limit 3 with cursor
    r2 = client.get(f"/feed?limit=3&cursor={p1['next_cursor']}")
    assert r2.status_code == 200
    p2 = r2.json()
    assert len(p2["posts"]) == 3
    assert p2["next_cursor"] is not None

    # Page 3: limit 3 with cursor
    r3 = client.get(f"/feed?limit=3&cursor={p2['next_cursor']}")
    assert r3.status_code == 200
    p3 = r3.json()
    assert len(p3["posts"]) == 1
    assert p3["next_cursor"] is None


def test_search_endpoint(client: TestClient):
    client.post("/agents", json={"name": "Doc Extractor", "function": "Extracts document text"})

    client.post("/posts/sync", json={
        "agent_slug": "doc-extractor",
        "text": "Extracted invoice data from PDF files",
        "tags": ["pdf", "finance"],
    })
    client.post("/posts/sync", json={
        "agent_slug": "doc-extractor",
        "text": "Compiled quarterly spreadsheet summary",
        "tags": ["spreadsheet", "finance"],
    })

    r = client.get("/search?q=invoice")
    assert r.status_code == 200
    results = r.json()
    assert len(results) == 1
    assert "invoice data" in results[0]["text"]


def test_follows_flow(client: TestClient):
    client.post("/agents", json={"name": "DevOps Bot", "function": "Automation"})
    headers = {"X-Viewer": "bob-engineer"}

    # Follow
    r_follow = client.post("/follows", json={"agent_slug": "devops-bot"}, headers=headers)
    assert r_follow.status_code == 201
    assert r_follow.json()["viewer"] == "bob-engineer"
    assert r_follow.json()["agent_slug"] == "devops-bot"

    # List follows
    r_list = client.get("/follows", headers=headers)
    assert r_list.status_code == 200
    follows = r_list.json()
    assert len(follows) == 1

    # Unfollow
    r_unfollow = client.delete("/follows/devops-bot", headers=headers)
    assert r_unfollow.status_code == 200
    assert r_unfollow.json()["status"] == "unfollowed"

    # List again -> 0
    r_list2 = client.get("/follows", headers=headers)
    assert len(r_list2.json()) == 0


def test_saved_views_flow(client: TestClient):
    client.post("/agents", json={"name": "Monitor Bot", "function": "Monitors services"})
    client.post("/posts/sync", json={
        "agent_slug": "monitor-bot",
        "text": "Server outage alert",
        "kind": "failed",
    })
    headers = {"X-Viewer": "carol-ops"}

    # Create saved view
    view_data = {
        "name": "Outages Only",
        "filters": {"kind": "failed"},
    }
    r_view = client.post("/views", json=view_data, headers=headers)
    assert r_view.status_code == 201
    created_view = r_view.json()
    view_id = created_view["id"]

    # List views
    r_views = client.get("/views", headers=headers)
    assert r_views.status_code == 200
    assert len(r_views.json()) == 1

    # Apply saved view
    r_apply = client.get(f"/views/{view_id}/apply", headers=headers)
    assert r_apply.status_code == 200
    applied_feed = r_apply.json()
    assert len(applied_feed["posts"]) == 1
    assert applied_feed["posts"][0]["kind"] == "failed"

    # Delete view
    r_del = client.delete(f"/views/{view_id}", headers=headers)
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "deleted"


def test_static_ui_served(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "Agentic Message Board" in r.text
