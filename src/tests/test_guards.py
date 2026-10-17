"""
test_guards.py — Guard tests to ensure zero hard-coding and scalability.

Requirements from spec:
  (1) Scan src/ for every agent name found in agents/*.yaml and fail if any
      appears in code (excluding agents/ specs and test fixtures).
  (2) Register 200 generated agents at runtime and verify feed/search still
      work with no code changes.
"""
from __future__ import annotations

from pathlib import Path
import yaml
import pytest

from board.config import AGENTS_DIR, SRC_DIR
from board.models import AgentRegister, PostCreate, FeedQuery
from board.store.sqlite import SQLiteStore


def test_guard_no_hardcoded_agent_names():
    """Scan src/ to ensure no agent names from YAML files are hard-coded in code."""
    yaml_files = list(AGENTS_DIR.glob("*.yaml")) + list(AGENTS_DIR.glob("*.yml"))
    if not yaml_files:
        # If no agent yaml files exist yet, nothing to violate
        return

    agent_names: set[str] = set()
    for yf in yaml_files:
        data = yaml.safe_load(yf.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "name" in data:
            agent_names.add(data["name"])

    assert len(agent_names) > 0

    # Files to inspect in src/
    code_files = []
    for p in SRC_DIR.rglob("*.py"):
        # Exclude tests/ directory and agents/ directory itself
        rel = p.relative_to(SRC_DIR)
        parts = rel.parts
        if "tests" in parts or "agents" in parts:
            continue
        code_files.append(p)

    violations = []
    for cf in code_files:
        content = cf.read_text(encoding="utf-8")
        for name in agent_names:
            if name in content:
                violations.append(f"Found hard-coded agent name '{name}' in {cf}")

    assert not violations, "\n".join(violations)


def test_guard_scale_200_agents():
    """Register 200 dynamically generated agents and verify feed and search work."""
    store = SQLiteStore(":memory:")
    store.init()

    try:
        # 1. Register 200 agents dynamically
        for i in range(200):
            store.register_agent(AgentRegister(
                name=f"Dynamic Autonomous Agent {i:03d}",
                function=f"Processes domain partition {i:03d} asynchronously",
                tags=[f"partition-{i % 10}", "scale-test"],
            ))

        all_agents = store.list_agents()
        assert len(all_agents) == 200

        # 2. Add posts for a subset of them
        for i in range(50):
            slug = all_agents[i].slug
            store.create_post(PostCreate(
                agent_slug=slug,
                text=f"Autoscale workload checkpoint for node {i}",
                tags=["scale-test", f"partition-{i % 10}"],
            ))

        # 3. Verify feed retrieval and filtering
        feed = store.get_feed(FeedQuery(tag="scale-test", limit=50))
        assert len(feed.posts) == 50

        # Filter by one specific partition
        part_feed = store.get_feed(FeedQuery(tag="partition-0", limit=50))
        assert len(part_feed.posts) == 5

        # 4. Verify search across dynamically scaled agents
        search_res = store.search("workload checkpoint")
        assert len(search_res) > 0
    finally:
        store.close()
