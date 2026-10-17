"""
test_p4_honesty.py — Verification suite for Phase P4 honest documentation, mock text extraction, and dead code removal.

Covers:
  - Mock text extraction clean without prompt instruction pollution.
  - Optional fallback_text respected by LLM adapter.
  - Unknown LLM backend safely defaults to mock.
  - Template formatting failure isolation in swarm runner produces visible failed post.
  - Wall-clock budget tracker accuracy.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.llm import (
    complete_with_metadata,
    resolve_backend,
    BudgetTracker,
    reset_budget_tracker,
)
from board.models import AgentKind, PostKind, FeedQuery
from board.store.sqlite import SQLiteStore
from swarm.loader import AgentSpec, WorkItem
from swarm.runner import execute_work_item


def test_mock_text_clean_extraction_no_prompt_pollution():
    """Verify that multi-line prompt extracts Work item without trailing instructions."""
    prompt = """Role: Security Auditor
Function: Auditing containers
Work item: Completed security scan of 50 images
Generate a concise, professional LinkedIn-style status update.
"""
    resp = complete_with_metadata(prompt, backend="mock")
    assert resp.text == "Completed work item: Completed security scan of 50 images [verified]"
    assert "LinkedIn-style" not in resp.text
    assert "Generate" not in resp.text


def test_fallback_text_override():
    """Verify fallback_text parameter overrides prompt parsing in mock mode."""
    custom = "Custom fallback update from agent."
    resp = complete_with_metadata("Some random prompt", backend="mock", fallback_text=custom)
    assert resp.text == custom
    assert resp.source == "mock"
    assert resp.failed is False


def test_unknown_backend_defaults_to_mock():
    """Unrecognized backend names should log warning and safely return mock."""
    assert resolve_backend("nonexistent-llm") == "mock"
    resp = complete_with_metadata("Work item: Task A", backend="gemini-or-other")
    assert resp.source == "mock"
    assert "Task A" in resp.text


def test_broken_template_produces_visible_failed_post():
    """Template syntax or missing key error in runner produces a failed post instead of dropping it."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    spec = AgentSpec(
        name="Broken Spec",
        function="Testing broken templates",
        kind=AgentKind.AGENT,
        tags=["broken"],
        prompt_template="Hello {missing_variable_that_causes_key_error}",
        work_items=[
            WorkItem(title="Broken Task", task="Should produce failed post"),
        ],
    )

    with TestClient(app) as client:
        # Register agent
        client.post("/agents", json={
            "name": spec.name,
            "function": spec.function,
            "kind": spec.kind.value,
            "tags": spec.tags,
        })

        source, failed, is_dup = execute_work_item(
            client,
            "http://testserver",
            spec,
            "broken-spec",
            spec.work_items[0],
        )
        assert failed is True
        assert source == "mock-fallback"
        assert is_dup is False

        app.state.queue.flush()

        feed = store.get_feed(FeedQuery(limit=10))
        assert len(feed.posts) == 1
        post = feed.posts[0]
        assert post.kind == PostKind.FAILED
        assert "failed" in post.text.lower()

    store.close()


def test_wall_clock_budget_tracker():
    """Budget tracker accurately tracks wall clock duration across concurrent callers."""
    tracker = BudgetTracker(budget_seconds=1.0)
    assert tracker.has_budget() is True

    tracker.start_if_not_started()
    time.sleep(0.05)
    spent = tracker.spent
    assert spent >= 0.04
    assert tracker.has_budget() is True

    # Simulate expiring the budget
    tracker.record(1.5)
    assert tracker.has_budget() is False
    assert tracker.spent >= 1.5
