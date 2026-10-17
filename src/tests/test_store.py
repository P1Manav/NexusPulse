"""
test_store.py — Unit tests for Store implementation (SQLiteStore).

Covers:
  - Agent registration, upsert, and listing.
  - Post creation and batch creation.
  - Keyset cursor pagination (ensuring no duplicate items or offset).
  - Feed filtering (by agent, tag, kind, since, q).
  - Search via FTS5 and LIKE fallback path.
  - Follows and Saved Views.
"""
from __future__ import annotations

import pytest

from board.models import (
    AgentKind, AgentRegister, PostCreate, PostKind, FeedQuery,
    FollowCreate, SavedViewCreate,
)
from board.store.sqlite import SQLiteStore
from board.feed import encode_cursor, decode_cursor


@pytest.fixture
def store():
    """Provides a fresh, isolated in-memory SQLite store."""
    s = SQLiteStore(":memory:")
    s.init()
    yield s
    s.close()


def test_store_health(store: SQLiteStore):
    assert store.healthy() is True
    assert store.get_agent("non-existent") is None


def test_agent_registration_and_upsert(store: SQLiteStore):
    # Register an agent
    reg = AgentRegister(
        name="Data Analyzer Alpha",
        function="Analyzes tabular data and extracts summary metrics",
        kind=AgentKind.AGENT,
        tags=["data", "analytics"],
    )
    agent = store.register_agent(reg)
    assert agent.slug == "data-analyzer-alpha"
    assert agent.name == "Data Analyzer Alpha"
    assert agent.kind == AgentKind.AGENT
    assert agent.tags == ["data", "analytics"]

    # Upsert with new function and tags
    reg_updated = AgentRegister(
        name="Data Analyzer Alpha",
        function="Updated function: high-performance statistical analyzer",
        kind=AgentKind.SKILL,
        tags=["data", "statistics", "fast"],
    )
    updated = store.register_agent(reg_updated)
    assert updated.slug == "data-analyzer-alpha"
    assert updated.function == "Updated function: high-performance statistical analyzer"
    assert updated.kind == AgentKind.SKILL
    assert "statistics" in updated.tags
    assert updated.created_at == agent.created_at

    # Listing agents
    agents = store.list_agents()
    assert len(agents) == 1
    assert agents[0].slug == "data-analyzer-alpha"


def test_posts_and_batch_creation(store: SQLiteStore):
    reg = AgentRegister(name="Worker One", function="Performs background jobs")
    agent = store.register_agent(reg)

    # Single post
    post = store.create_post(PostCreate(
        agent_slug=agent.slug,
        text="Starting task #101",
        kind=PostKind.UPDATE,
        tags=["jobs", "task-101"],
        metadata={"run_id": 101},
        source="mock",
    ))
    assert post.id is not None
    assert post.agent_slug == agent.slug
    assert post.kind == PostKind.UPDATE

    # Batch posts
    batch_items = [
        PostCreate(
            agent_slug=agent.slug,
            text=f"Batch update {i}",
            kind=PostKind.UPDATE,
            tags=["batch"],
        )
        for i in range(5)
    ]
    posts = store.create_posts_batch(batch_items)
    assert len(posts) == 5

    feed = store.get_feed(FeedQuery(limit=20))
    # 1 single + 5 batch = 6 total posts
    assert len(feed.posts) == 6


def test_keyset_cursor_pagination(store: SQLiteStore):
    reg = store.register_agent(AgentRegister(name="Paginator Agent", function="Produces ordered updates"))

    # Create 15 posts
    for i in range(15):
        store.create_post(PostCreate(
            agent_slug=reg.slug,
            text=f"Message index {i:02d}",
            kind=PostKind.UPDATE,
        ))

    # Page 1: limit 5
    page1 = store.get_feed(FeedQuery(limit=5))
    assert len(page1.posts) == 5
    assert page1.next_cursor is not None

    # Page 2: limit 5 using page1 cursor
    page2 = store.get_feed(FeedQuery(limit=5, cursor=page1.next_cursor))
    assert len(page2.posts) == 5
    assert page2.next_cursor is not None

    # Page 3: limit 5
    page3 = store.get_feed(FeedQuery(limit=5, cursor=page2.next_cursor))
    assert len(page3.posts) == 5
    # Since there were 15 posts, page3 has no more
    assert page3.next_cursor is None

    # Ensure all posts across pages are distinct (no duplicates)
    all_ids = [p.id for p in page1.posts] + [p.id for p in page2.posts] + [p.id for p in page3.posts]
    assert len(set(all_ids)) == 15
    # Verify ordering is strictly descending (newest first)
    int_ids = [int(i) for i in all_ids]
    assert int_ids == sorted(int_ids, reverse=True)


def test_feed_filtering(store: SQLiteStore):
    a1 = store.register_agent(AgentRegister(name="Agent Alpha", function="Alpha service"))
    a2 = store.register_agent(AgentRegister(name="Agent Beta", function="Beta service"))

    store.create_post(PostCreate(
        agent_slug=a1.slug,
        text="Alpha pipeline started",
        kind=PostKind.UPDATE,
        tags=["pipeline", "ci"],
    ))
    store.create_post(PostCreate(
        agent_slug=a1.slug,
        text="Alpha milestone reached",
        kind=PostKind.MILESTONE,
        tags=["milestone", "release"],
    ))
    store.create_post(PostCreate(
        agent_slug=a2.slug,
        text="Beta encountered error",
        kind=PostKind.FAILED,
        tags=["error", "pipeline"],
    ))

    # Filter by agent
    alpha_feed = store.get_feed(FeedQuery(agent=a1.slug))
    assert len(alpha_feed.posts) == 2
    assert all(p.agent_slug == a1.slug for p in alpha_feed.posts)

    # Filter by tag
    pipeline_feed = store.get_feed(FeedQuery(tag="pipeline"))
    assert len(pipeline_feed.posts) == 2

    # Filter by kind
    failed_feed = store.get_feed(FeedQuery(kind=PostKind.FAILED))
    assert len(failed_feed.posts) == 1
    assert failed_feed.posts[0].kind == PostKind.FAILED

    # Filter by text search q in feed
    error_feed = store.get_feed(FeedQuery(q="encountered"))
    assert len(error_feed.posts) == 1
    assert "Beta encountered error" in error_feed.posts[0].text


def test_fts5_and_fallback_search(store: SQLiteStore):
    agent = store.register_agent(AgentRegister(name="Search Tester", function="Indexing verification"))

    store.create_post(PostCreate(
        agent_slug=agent.slug,
        text="Deploying Kubernetes cluster with zero downtime",
        tags=["k8s", "deploy"],
    ))
    store.create_post(PostCreate(
        agent_slug=agent.slug,
        text="SQLite FTS5 full-text search indexing is blazingly fast",
        tags=["database", "sqlite"],
    ))
    store.create_post(PostCreate(
        agent_slug=agent.slug,
        text="Regular routine checkup completed successfully",
        tags=["maintenance"],
    ))

    # Test FTS5 search (if FTS5 compiled into sqlite)
    results = store.search("Kubernetes")
    assert len(results) == 1
    assert "Kubernetes cluster" in results[0].text

    # Explicitly test LIKE fallback search method
    like_results = store._search_like("FTS5", limit=10)
    assert len(like_results) == 1
    assert "SQLite FTS5" in like_results[0].text

    # Simulate store without FTS5
    store._fts_available = False
    assert store.has_fts() is False
    fallback_results = store.search("routine")
    assert len(fallback_results) == 1
    assert "routine checkup" in fallback_results[0].text


def test_follows_and_saved_views(store: SQLiteStore):
    agent1 = store.register_agent(AgentRegister(name="Service Hub", function="Routes messages"))
    agent2 = store.register_agent(AgentRegister(name="Log Collector", function="Aggregates logs"))

    viewer = "alice-dev"

    # Follow
    follow1 = store.add_follow(viewer, FollowCreate(agent_slug=agent1.slug))
    assert follow1.viewer == viewer
    assert follow1.agent_slug == agent1.slug

    # Idempotent follow
    store.add_follow(viewer, FollowCreate(agent_slug=agent1.slug))
    store.add_follow(viewer, FollowCreate(agent_slug=agent2.slug))

    follows = store.list_follows(viewer)
    assert len(follows) == 2
    slugs = {f.agent_slug for f in follows}
    assert agent1.slug in slugs and agent2.slug in slugs

    # Unfollow
    removed = store.remove_follow(viewer, agent1.slug)
    assert removed is True
    assert len(store.list_follows(viewer)) == 1

    # Unfollow non-existent
    assert store.remove_follow(viewer, "unknown-agent") is False

    # Saved view
    view = store.create_saved_view(viewer, SavedViewCreate(
        name="Failures View",
        filters={"kind": "failed", "tag": "alert"},
    ))
    assert view.id is not None
    assert view.name == "Failures View"
    assert view.filters["kind"] == "failed"

    # List views
    views = store.list_saved_views(viewer)
    assert len(views) == 1
    assert views[0].id == view.id

    # Get view
    fetched = store.get_saved_view(viewer, view.id)
    assert fetched is not None
    assert fetched.name == "Failures View"

    # Delete view
    assert store.delete_saved_view(viewer, view.id) is True
    assert store.get_saved_view(viewer, view.id) is None
