"""
test_phase_a_last.py — Verification of Phase A fixes (Last Pass).

Covers:
  A1. /search timezone normalization in since (FTS and LIKE paths), future date, and 422 on invalid timestamp.
  A2. swarm CLI loud exit codes: 1 on unreachable API, 1 on submit error, 0 on success.
  A3. Failed-post namespace and daily accumulation cap: max 1 failed post per work item per day.
  A4. Legacy DB FTS rebuild for searchable old posts.
  A5. Benchmark scenario 5 renamed to '5. Mixed Load (12 workers, 1/3 writes)'.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from board import config
from board.api import create_app
from board.llm import LLMResponse
from board.models import AgentRegister, PostCreate, FeedQuery
from board.store.sqlite import SQLiteStore
from swarm.loader import AgentSpec, WorkItem, load_all_agent_specs
from swarm.runner import run_swarm, run_swarm_cli, SwarmResult


def test_a1_search_since_timezone_and_validation():
    """A1: /search supports timezones in since (+05:30, -08:00, future, garbage)."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store, enable_sync=True)
    client = TestClient(app)

    # Register an agent and post
    client.post("/agents", json={"name": "Timezone Probe", "function": "Testing tz", "kind": "agent"})
    post_res = client.post("/posts/sync", json={
        "agent_slug": "timezone-probe",
        "text": "Critical incident alert resolved",
        "kind": "update",
        "tags": ["alert"],
    })
    assert post_res.status_code == 201

    # 10 minutes ago in UTC
    now_utc = dt.datetime.now(dt.timezone.utc)
    ten_min_ago = now_utc - dt.timedelta(minutes=10)

    # Offset +05:30
    tz_plus = dt.timezone(dt.timedelta(hours=5, minutes=30))
    since_plus = ten_min_ago.astimezone(tz_plus).isoformat()

    # Offset -08:00
    tz_minus = dt.timezone(dt.timedelta(hours=-8))
    since_minus = ten_min_ago.astimezone(tz_minus).isoformat()

    # Future: 10 minutes from now
    since_future = (now_utc + dt.timedelta(minutes=10)).isoformat()

    # Test FTS path
    res_plus = client.get(f"/search?q=incident&since={since_plus}")
    assert res_plus.status_code == 200
    assert len(res_plus.json()) == 1

    res_minus = client.get(f"/search?q=incident&since={since_minus}")
    assert res_minus.status_code == 200
    assert len(res_minus.json()) == 1

    res_fut = client.get(f"/search?q=incident&since={since_future}")
    assert res_fut.status_code == 200
    assert len(res_fut.json()) == 0

    # Test LIKE fallback path (simulate _fts_available=False)
    store._fts_available = False
    res_like_plus = client.get(f"/search?q=incident&since={since_plus}")
    assert res_like_plus.status_code == 200
    assert len(res_like_plus.json()) == 1

    res_like_minus = client.get(f"/search?q=incident&since={since_minus}")
    assert res_like_minus.status_code == 200
    assert len(res_like_minus.json()) == 1

    res_like_fut = client.get(f"/search?q=incident&since={since_future}")
    assert res_like_fut.status_code == 200
    assert len(res_like_fut.json()) == 0

    store._fts_available = True

    # Invalid timestamp returns 422 on both /feed and /search
    res_feed_bad = client.get("/feed?since=garbage_timestamp")
    assert res_feed_bad.status_code == 422
    assert "Invalid 'since' timestamp" in res_feed_bad.json()["detail"]

    res_search_bad = client.get("/search?q=incident&since=garbage_timestamp")
    assert res_search_bad.status_code == 422
    assert "Invalid 'since' timestamp" in res_search_bad.json()["detail"]

    store.close()


def test_a2_swarm_cli_exit_codes(monkeypatch, capsys):
    """A2: run_swarm_cli returns 1 when API is unreachable or on submit error; 0 on healthy run."""
    # 1. Unreachable API -> exits 1 within 1 second
    monkeypatch.setattr(config, "SWARM_WAIT_SECONDS", 1)
    with patch("httpx.get", side_effect=Exception("Connection refused")):
        code = run_swarm_cli()
        assert code == 1
        captured = capsys.readouterr().out
        assert "Cannot connect to API server" in captured

    # 2. Healthy run -> returns 0
    with patch("httpx.get", return_value=MagicMock(status_code=200)):
        with patch("swarm.runner.run_swarm") as mock_run:
            mock_res = SwarmResult()
            mock_res.posts_failed = 0
            mock_run.return_value = mock_res
            code = run_swarm_cli()
            assert code == 0

    # 3. Submit error during swarm run -> returns 1
    with patch("httpx.get", return_value=MagicMock(status_code=200)):
        with patch("swarm.runner.run_swarm") as mock_run:
            mock_res = SwarmResult()
            mock_res.posts_failed = 2  # Submit errors encountered
            mock_run.return_value = mock_res
            code = run_swarm_cli()
            assert code == 1


def test_a3_failed_post_daily_accumulation_cap():
    """A3: Two runs with failing LLM create exactly 20 failed posts total; subsequent healthy run creates 20 normal posts."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    specs = load_all_agent_specs()
    assert len(specs) == 10
    total_items = sum(len(s.work_items) for s in specs)
    assert total_items == 20

    failing_response = LLMResponse(
        text="Temporary failure [LLM unavailable: using fallback]",
        source="mock-fallback",
        failed=True,
        error_category="timeout",
    )

    # Run 1: failing backend -> 20 failed posts created
    with patch("swarm.runner.complete_with_metadata", return_value=failing_response):
        res1 = run_swarm("http://testserver", specs=specs, client=client)
    assert res1.posts_submitted == 20
    assert res1.posts_skipped == 0
    app.state.queue.flush()

    feed1 = client.get("/feed?limit=100").json()
    assert len(feed1["posts"]) == 20
    for p in feed1["posts"]:
        assert p["kind"] == "failed"
        assert p["dedupe_key"].startswith("failed-")

    # Run 2: failing backend on the same day -> 0 new posts, 20 skipped
    with patch("swarm.runner.complete_with_metadata", return_value=failing_response):
        res2 = run_swarm("http://testserver", specs=specs, client=client)
    assert res2.posts_submitted == 0
    assert res2.posts_skipped == 20
    app.state.queue.flush()

    feed2 = client.get("/feed?limit=100").json()
    assert len(feed2["posts"]) == 20  # Still 20, not 40!

    # Run 3: healthy backend -> 20 normal posts created
    healthy_response = LLMResponse(
        text="Normal operational update succeeded [verified]",
        source="mock",
        failed=False,
    )
    with patch("swarm.runner.complete_with_metadata", return_value=healthy_response):
        res3 = run_swarm("http://testserver", specs=specs, client=client)
    assert res3.posts_submitted == 20
    assert res3.posts_skipped == 0
    app.state.queue.flush()

    feed3 = client.get("/feed?limit=100").json()
    assert len(feed3["posts"]) == 40  # 20 failed + 20 successful normal posts

    store.close()


def test_a4_legacy_db_fts_rebuild(tmp_path):
    """A4: Legacy SQLite file without FTS5 entries rebuilds FTS on init and search('searchable') succeeds."""
    db_file = tmp_path / "legacy_search.db"
    raw_conn = sqlite3.connect(str(db_file))
    raw_conn.executescript("""
        CREATE TABLE agents (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            function TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'skill',
            tags TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_slug TEXT NOT NULL,
            text TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'update',
            tags TEXT NOT NULL DEFAULT '[]',
            metadata TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'mock',
            created_at TEXT NOT NULL
        );
    """)
    raw_conn.execute("INSERT INTO agents VALUES ('legacy-agent', 'Legacy Agent', 'Testing', 'agent', '[]', '2026-01-01', '2026-01-01')")
    raw_conn.execute("INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, created_at) VALUES ('legacy-agent', 'This is a searchable post from an older version', 'update', '[]', '{}', 'mock', '2026-01-01')")
    raw_conn.commit()
    raw_conn.close()

    # Open with store
    store = SQLiteStore(str(db_file))
    store.init()
    try:
        results = store.search("searchable")
        assert len(results) == 1
        assert "searchable" in results[0].text
    finally:
        store.close()


def test_a5_bench_label_mixed_load():
    """A5: Benchmark scenario 5 is named '5. Mixed Load (12 workers, 1/3 writes)'."""
    from board.bench import run_bench
    # Run bench with small operations via mock or inspecting scenario definitions in source code
    import inspect
    import board.bench as bench_mod
    src = inspect.getsource(bench_mod.run_benchmarks)
    assert '5. Mixed Load (12 workers, 1/3 writes)' in src
