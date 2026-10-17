"""
test_customization.py — Deep tests for follows, saved views, and pagination edge cases.

Requirements tested:
  - Viewer and Agent-to-Agent follows via X-Viewer header.
  - Personalized Follow Feed (GET /feed?following=true).
  - Viewer isolation for saved views.
  - Keyset cursor pagination error handling (malformed cursor -> 400).
  - Keyset cursor boundary conditions (empty end, multi-filter combinations).
  - Timestamp filtering via 'since' parameter.
"""
from __future__ import annotations

import datetime as dt
import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.models import AgentRegister, PostCreate, PostKind
from board.store.sqlite import SQLiteStore


@pytest.fixture
def client():
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)
    with TestClient(app) as test_client:
        yield test_client
    store.close()


def test_agent_to_agent_following(client: TestClient):
    """Ensure an agent can follow another agent via X-Viewer header."""
    client.post("/agents", json={"name": "Architect Agent", "function": "Designs systems"})
    client.post("/agents", json={"name": "Builder Agent", "function": "Executes builds"})

    # Builder Agent follows Architect Agent
    headers = {"X-Viewer": "builder-agent"}
    r = client.post("/follows", json={"agent_slug": "architect-agent"}, headers=headers)
    assert r.status_code == 201
    assert r.json()["viewer"] == "builder-agent"
    assert r.json()["agent_slug"] == "architect-agent"

    # Verify follow is listed
    r_list = client.get("/follows", headers=headers)
    assert r_list.status_code == 200
    follows = r_list.json()
    assert len(follows) == 1
    assert follows[0]["agent_slug"] == "architect-agent"


def test_personalized_follow_feed(client: TestClient):
    """Ensure GET /feed?following=true restricts results to followed agents only."""
    client.post("/agents", json={"name": "Alpha Service", "function": "Alpha tasks"})
    client.post("/agents", json={"name": "Beta Service", "function": "Beta tasks"})
    client.post("/agents", json={"name": "Gamma Service", "function": "Gamma tasks"})

    # Post from each
    client.post("/posts/sync", json={"agent_slug": "alpha-service", "text": "Alpha update 1"})
    client.post("/posts/sync", json={"agent_slug": "beta-service", "text": "Beta update 1"})
    client.post("/posts/sync", json={"agent_slug": "gamma-service", "text": "Gamma update 1"})

    viewer_headers = {"X-Viewer": "user-dave"}

    # Initially user-dave follows no one -> following feed should be empty
    r_empty = client.get("/feed?following=true", headers=viewer_headers)
    assert r_empty.status_code == 200
    assert len(r_empty.json()["posts"]) == 0

    # user-dave follows only Beta Service
    client.post("/follows", json={"agent_slug": "beta-service"}, headers=viewer_headers)

    # Now following feed should contain ONLY Beta's post
    r_following = client.get("/feed?following=true", headers=viewer_headers)
    assert r_following.status_code == 200
    posts = r_following.json()["posts"]
    assert len(posts) == 1
    assert posts[0]["agent_slug"] == "beta-service"

    # user-dave also follows Gamma Service
    client.post("/follows", json={"agent_slug": "gamma-service"}, headers=viewer_headers)
    r_two = client.get("/feed?following=true", headers=viewer_headers)
    assert len(r_two.json()["posts"]) == 2
    slugs = {p["agent_slug"] for p in r_two.json()["posts"]}
    assert slugs == {"beta-service", "gamma-service"}


def test_saved_view_viewer_isolation(client: TestClient):
    """Ensure viewers cannot see or delete another viewer's saved views."""
    client.post("/agents", json={"name": "Shared Agent", "function": "Workload handler"})

    headers_alice = {"X-Viewer": "alice"}
    headers_bob = {"X-Viewer": "bob"}

    # Alice creates a view
    r_alice = client.post("/views", json={"name": "Alice View", "filters": {"kind": "milestone"}}, headers=headers_alice)
    view_id = r_alice.json()["id"]

    # Bob lists views -> should be empty
    r_bob_list = client.get("/views", headers=headers_bob)
    assert len(r_bob_list.json()) == 0

    # Bob tries to get Alice's view -> 404
    r_bob_get = client.get(f"/views/{view_id}", headers=headers_bob)
    assert r_bob_get.status_code == 404

    # Bob tries to delete Alice's view -> 404
    r_bob_del = client.delete(f"/views/{view_id}", headers=headers_bob)
    assert r_bob_del.status_code == 404

    # Alice can get her view
    r_alice_get = client.get(f"/views/{view_id}", headers=headers_alice)
    assert r_alice_get.status_code == 200
    assert r_alice_get.json()["name"] == "Alice View"


def test_cursor_malformed_returns_400(client: TestClient):
    """Malformed or invalid base64 cursor must return 400 Bad Request, not 500."""
    client.post("/agents", json={"name": "Test Paging", "function": "Paging agent"})
    client.post("/posts/sync", json={"agent_slug": "test-paging", "text": "Post 1"})

    # Send gibberish cursor
    r = client.get("/feed?cursor=invalid_not_base64!!!")
    assert r.status_code == 400
    assert "Invalid pagination cursor" in r.json()["detail"]


def test_pagination_boundary_conditions(client: TestClient):
    """Ensure keyset cursor reaches end with next_cursor=None and empty page beyond."""
    client.post("/agents", json={"name": "Boundary Agent", "function": "Paging edge cases"})

    for i in range(4):
        client.post("/posts/sync", json={"agent_slug": "boundary-agent", "text": f"Boundary #{i}"})

    # Page 1: 2 items
    p1 = client.get("/feed?limit=2").json()
    assert len(p1["posts"]) == 2
    c1 = p1["next_cursor"]
    assert c1 is not None

    # Page 2: remaining 2 items
    p2 = client.get(f"/feed?limit=2&cursor={c1}").json()
    assert len(p2["posts"]) == 2
    c2 = p2["next_cursor"]
    assert c2 is None  # End reached!

    # Page 3: query with encoded id of oldest post -> empty results
    from board.feed import encode_cursor
    oldest_id = int(p2["posts"][-1]["id"])
    end_cursor = encode_cursor(oldest_id)
    p3 = client.get(f"/feed?limit=2&cursor={end_cursor}").json()
    assert len(p3["posts"]) == 0
    assert p3["next_cursor"] is None


def test_since_timestamp_filtering(client: TestClient):
    """Verify 'since' filter excludes older posts."""
    client.post("/agents", json={"name": "Time Agent", "function": "Chronological tests"})

    past_time = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    now_time = dt.datetime.now(dt.timezone.utc).isoformat()

    client.post("/posts/sync", json={"agent_slug": "time-agent", "text": "Current post"})

    # Feed since past_time should include current post
    r1 = client.get(f"/feed?since={past_time}")
    assert len(r1.json()["posts"]) == 1

    # Feed since future time should exclude current post
    future_time = dt.datetime(2099, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    r2 = client.get(f"/feed?since={future_time}")
    assert len(r2.json()["posts"]) == 0
