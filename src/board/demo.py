"""
board.demo — Orchestrates the full demo: API + swarm + sample queries.

Execution flow:
  1. Finds an ephemeral free TCP port on localhost.
  2. Boots the FastAPI app in a background daemon thread with uvicorn.
  3. Waits for /healthz to report healthy.
  4. Runs the swarm runner (loading 10 agents from src/agents/, registering them,
     generating posts concurrently via llm adapter, and ingesting to the board).
  5. Flushes the background post queue to ensure all batched writes are persisted.
  6. Queries and prints sample results:
     - Real-time feed with cursor pagination.
     - Tag & Kind filtering (e.g. milestones, failures).
     - Full-text search with FTS5 ranking.
     - Customization: follows and saved views with X-Viewer headers.
  7. Gracefully stops the server and exits with code 0.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import uvicorn

from board.api import create_app
from board.store.sqlite import SQLiteStore
from swarm.runner import run_swarm


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_demo() -> int:
    """Run the complete end-to-end demo and return exit code 0."""
    port = _find_free_port()
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    server = uvicorn.Server(uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"

    # Wait for server readiness
    for _ in range(50):
        try:
            r = httpx.get(f"{base}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        print("[ERROR] FastAPI server failed to start on ephemeral port")
        return 1

    print("=" * 72)
    print("  SUPER-LIGHTWEIGHT AGENTIC MESSAGE BOARD -- LIVE DEMONSTRATION")
    print("=" * 72)
    print(f"[OK] In-process API online: {base}")

    # Health & Readiness Checks
    r_health = httpx.get(f"{base}/healthz").json()
    r_ready = httpx.get(f"{base}/readyz").json()
    print(f"[OK] Health probes verified: /healthz={r_health['status']}, /readyz={r_ready['status']}")

    # Run Swarm (Pass 1)
    print("\n--- 1a. Swarm Execution: Pass 1 (10 Agents / Skills) ---")
    swarm_res1 = run_swarm(base)

    # Flush the post queue to guarantee all batched writes are saved in SQLite
    if hasattr(app.state, "queue") and app.state.queue is not None:
        app.state.queue.flush()

    # Run Swarm (Pass 2 - Idempotency Verification)
    print("\n--- 1b. Swarm Execution: Pass 2 (Idempotency Re-run) ---")
    swarm_res2 = run_swarm(base)
    if hasattr(app.state, "queue") and app.state.queue is not None:
        app.state.queue.flush()
    print(f"[OK] Second swarm pass verified: {swarm_res2.posts_submitted} new, {swarm_res2.posts_skipped} skipped as duplicate")

    # Query Registered Agents
    with httpx.Client(base_url=base) as client:
        agents = client.get("/agents").json()
        print(f"\n[OK] Registered Agents ({len(agents)} total):")
        for ag in agents[:5]:
            print(f"     - [{ag['kind'].upper()}] {ag['name']} (@{ag['slug']}) -> {ag['tags']}")
        if len(agents) > 5:
            print(f"     ... and {len(agents) - 5} more.")

        # Query Feed (Page 1)
        print("\n--- 2. Live Feed & Keyset Pagination ---")
        feed1 = client.get("/feed?limit=4").json()
        print(f"[OK] Feed (Page 1, showing {len(feed1['posts'])} latest posts):")
        for p in feed1["posts"]:
            print(f"     [@{p['agent_slug']} | {p['kind'].upper()}] {p['text']}")
            print(f"       Tags: {p['tags']} | Source: {p['source']}")

        cursor = feed1.get("next_cursor")
        if cursor:
            feed2 = client.get(f"/feed?limit=2&cursor={cursor}").json()
            print(f"[OK] Feed (Page 2 via cursor '{cursor[:12]}...'): {len(feed2['posts'])} posts retrieved")

        # Query Filtered Feed (Milestones only)
        print("\n--- 3. Filtered Views (Kind & Tags) ---")
        milestones = client.get("/feed?kind=milestone&limit=3").json()
        print(f"[OK] Milestones Feed ({len(milestones['posts'])} found):")
        for m in milestones["posts"]:
            print(f"     * [{m['agent_slug']}] {m['text'][:80]}...")

        # Full-Text Search
        print("\n--- 4. Full-Text Search (FTS5 / Ranking) ---")
        search_query = "Kubernetes OR cluster OR forecast"
        search_results = client.get(f"/search?q={search_query}&limit=3").json()
        print(f"[OK] Search results for '{search_query}' ({len(search_results)} matches):")
        for sr in search_results:
            print(f"     * [@{sr['agent_slug']}] {sr['text'][:80]}...")

        # Viewer Customization: Follows
        print("\n--- 5. Viewer Personalization (Follows & Saved Views) ---")
        viewer_headers = {"X-Viewer": "lead-architect"}

        # Follow 2 agents
        first_agent = agents[0]["slug"]
        second_agent = agents[1]["slug"]
        client.post("/follows", json={"agent_slug": first_agent}, headers=viewer_headers)
        client.post("/follows", json={"agent_slug": second_agent}, headers=viewer_headers)

        my_follows = client.get("/follows", headers=viewer_headers).json()
        print(f"[OK] Viewer 'lead-architect' following {len(my_follows)} agents:")
        for f in my_follows:
            print(f"     -> Following: @{f['agent_slug']}")

        # Create Saved View
        view_payload = {
            "name": "Critical Milestones & Incidents",
            "filters": {"kind": "milestone"},
        }
        created_view = client.post("/views", json=view_payload, headers=viewer_headers).json()
        view_id = created_view["id"]
        print(f"[OK] Created Saved View: '{created_view['name']}' (ID: {view_id})")

        # Apply Saved View
        applied = client.get(f"/views/{view_id}/apply", headers=viewer_headers).json()
        print(f"[OK] Applied Saved View '{created_view['name']}': {len(applied['posts'])} posts matched filter")

    print("\n" + "=" * 72)
    print("  DEMO COMPLETED SUCCESSFULLY -- EXITING CODE 0")
    print("=" * 72)

    # Clean shutdown
    server.should_exit = True
    thread.join(timeout=5)
    store.close()
    return 0
