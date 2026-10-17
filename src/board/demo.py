"""
board.demo — Orchestrates the full demo: API + swarm + sample queries.

Boots the API on a free port in-process, loads agent YAMLs, runs the swarm,
prints sample feed/search/follow/saved-view results, then exits with code 0.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import uvicorn


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_demo() -> int:
    """Run the full demo and return exit code 0 on success."""
    from board.api import create_app

    port = _find_free_port()
    app = create_app()

    # Start uvicorn in a background thread so it doesn't block
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            r = httpx.get(f"{base}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except httpx.ConnectError:
            time.sleep(0.1)
    else:
        print("ERROR: Server did not start in time")
        return 1

    print(f"[OK] API running on {base}")

    # -- Phase M0: just hit healthz --
    r = httpx.get(f"{base}/healthz")
    print(f"[OK] GET /healthz -> {r.json()}")

    r = httpx.get(f"{base}/readyz")
    print(f"[OK] GET /readyz  -> {r.json()}")

    print("")
    print("[OK] Demo complete (Phase M0 -- skeleton only)")
    print("  Next phases will add: agents, posts, feed, search, swarm, UI")

    # Shut down gracefully
    server.should_exit = True
    thread.join(timeout=5)
    return 0
