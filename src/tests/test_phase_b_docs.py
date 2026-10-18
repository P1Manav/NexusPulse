"""
test_phase_b_docs.py — Prevents documentation drift for curls, env vars, and metrics.

Covers:
  B1. README external agent registration, post, feed query, and search payloads.
  B2. README configuration table matches config.py environment variables bidirectionally.
  B3. README metrics list matches live /metrics Prometheus exposition output.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from board import config
from board.api import create_app
from board.config import SRC_DIR
from board.store.sqlite import SQLiteStore

REPO_ROOT = SRC_DIR.parent
README_PATH = REPO_ROOT / "README.md"


def test_b1_readme_curl_payloads_testclient():
    """B1: Exact JSON bodies from README work against TestClient (201 / 202 / 200)."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    # 1. Register agent
    agent_body = {
        "name": "External Auditor",
        "function": "Continuous vulnerability assessments",
        "kind": "agent",
        "tags": ["security", "compliance"],
    }
    r1 = client.post("/agents", json=agent_body)
    assert r1.status_code == 201
    assert r1.json()["slug"] == "external-auditor"

    # 2. Post
    post_body = {
        "agent_slug": "external-auditor",
        "text": "Zero high-severity vulnerabilities found across all endpoints.",
        "kind": "milestone",
        "tags": ["security", "audit"],
        "dedupe_key": "scan-2026-10-18",
    }
    r2 = client.post("/posts", json=post_body)
    assert r2.status_code == 202

    # Flush write queue to SQLite
    app.state.queue.flush()

    # 3. Query feed
    r3_feed = client.get("/feed?tag=security&limit=10")
    assert r3_feed.status_code == 200
    feed_posts = r3_feed.json()["posts"]
    assert len(feed_posts) == 1
    assert feed_posts[0]["agent_slug"] == "external-auditor"

    # 4. Search
    r3_search = client.get("/search?q=vulnerabilities")
    assert r3_search.status_code == 200
    search_posts = r3_search.json()
    assert len(search_posts) == 1
    assert "vulnerabilities" in search_posts[0]["text"]

    store.close()


def test_b2_readme_config_table_env_vars():
    """B2: Every env var in README table exists in config.py and vice versa for the documented set."""
    readme_text = README_PATH.read_text(encoding="utf-8")
    
    # Extract env vars from table: | `VAR_NAME` | ...
    table_vars = set(re.findall(r"\|\s*`([A-Z0-9_]+)`\s*\|", readme_text))
    
    # Config.py env vars from os.getenv
    config_path = SRC_DIR / "board" / "config.py"
    config_text = config_path.read_text(encoding="utf-8")
    config_vars = set(re.findall(r'os\.getenv\(["\']([A-Z0-9_]+)["\']', config_text))

    # All config.py env vars must be documented
    missing_in_readme = config_vars - table_vars
    assert not missing_in_readme, f"Env vars in config.py not documented in README: {missing_in_readme}"

    # All table vars must exist in config.py
    missing_in_config = table_vars - config_vars
    assert not missing_in_config, f"Env vars in README table not present in config.py: {missing_in_config}"


def test_b3_readme_metrics_list():
    """B3: Every metric name listed in the README appears in a live /metrics response."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)
    client = TestClient(app)

    metrics_text = client.get("/metrics").text
    readme_text = README_PATH.read_text(encoding="utf-8")

    # Find the Metrics Exposition section in README
    metrics_section_match = re.search(
        r"### Metrics Exposition.*?(?=\n---|\Z)",
        readme_text,
        re.DOTALL,
    )
    assert metrics_section_match is not None, "Metrics Exposition section missing from README"
    metrics_section = metrics_section_match.group(0)

    # Extract backticked metric names in that section (e.g. `posts_accepted_total`)
    documented_metrics = re.findall(r"^-\s*`([a-z0-9_]+)", metrics_section, re.MULTILINE)
    assert len(documented_metrics) >= 6, "Expected at least 6 documented metrics"

    # Make sure outdated metric names were removed
    assert "queue_size_current" not in documented_metrics
    assert "post_items_ingested_total" not in documented_metrics
    assert "active_agents_total" not in documented_metrics

    # Verify each appears in live Prometheus output
    for metric_name in documented_metrics:
        assert metric_name in metrics_text, f"Metric '{metric_name}' documented in README but not found in live /metrics"

    store.close()
