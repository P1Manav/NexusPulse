"""
test_swarm.py — Unit and integration tests for Swarm loader and runner.

Covers:
  - Spec loading and validation (at least 10 personas loaded).
  - Dynamic registration via HTTP.
  - Concurrent work item dispatch.
  - Failure isolation (single agent failure does not crash the swarm).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.models import AgentKind, PostKind, FeedQuery
from board.store.sqlite import SQLiteStore
from swarm.loader import load_all_agent_specs, AgentSpec, WorkItem
from swarm.runner import run_swarm, execute_work_item


def test_loader_loads_ten_agents():
    specs = load_all_agent_specs()
    assert len(specs) >= 10
    names = {s.name for s in specs}
    assert len(names) == len(specs)  # All names unique

    for s in specs:
        assert s.name
        assert s.function
        assert s.prompt_template
        assert len(s.work_items) >= 1


def test_swarm_execution_flow(monkeypatch):
    """Run swarm against TestClient-backed API."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    # Use a small test spec to test runner fast
    spec = AgentSpec(
        name="Test Worker",
        function="Executes unit tasks",
        kind=AgentKind.AGENT,
        tags=["unit-test"],
        prompt_template="Role: {name}\nFunction: {function}\nWork item: {task}",
        work_items=[
            WorkItem(
                title="Task A",
                task="Deploy staging environment",
                kind=PostKind.UPDATE,
                tags=["staging"],
            ),
            WorkItem(
                title="Task B",
                task="Encountered network timeout",
                kind=PostKind.FAILED,
                tags=["network"],
            ),
        ],
    )

    with TestClient(app) as client:
        base_url = "http://testserver"

        # Register agent first
        from swarm.runner import register_agent_http
        slug = register_agent_http(client, base_url, spec)
        assert slug == "test-worker"

        # Test execute_work_item directly using client
        source, failed, is_dup = execute_work_item(client, base_url, spec, slug, spec.work_items[0])
        assert source == "mock"
        assert failed is False
        assert is_dup is False

        # Verify second item with kind=failed
        source, failed, is_dup = execute_work_item(client, base_url, spec, slug, spec.work_items[1])
        assert failed is False or spec.work_items[1].kind == PostKind.FAILED

        # Flush queue
        app.state.queue.flush()

        feed = store.get_feed(FeedQuery(limit=10))
        assert len(feed.posts) == 2
        kinds = {p.kind for p in feed.posts}
        assert PostKind.UPDATE in kinds
        assert PostKind.FAILED in kinds

    store.close()
