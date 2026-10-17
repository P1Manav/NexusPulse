"""
board.store.base — Abstract Store interface.

Defines the contract that storage backends must implement.
The API layer depends ONLY on this interface, never on a concrete backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from board.models import (
    Agent, AgentRegister, Post, PostCreate,
    FeedQuery, FeedResponse,
    Follow, FollowCreate, SavedView, SavedViewCreate,
)


class Store(ABC):
    """Abstract storage backend for the message board."""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    @abstractmethod
    def init(self) -> None:
        """Create tables / indexes. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""

    @abstractmethod
    def healthy(self) -> bool:
        """Return True if the store is usable."""

    # ── Agents ─────────────────────────────────────────────────────────────

    @abstractmethod
    def register_agent(self, data: AgentRegister) -> Agent:
        """Idempotent upsert by slug. Returns the (possibly updated) agent."""

    @abstractmethod
    def get_agent(self, slug: str) -> Agent | None:
        """Return agent by slug, or None."""

    @abstractmethod
    def list_agents(self) -> list[Agent]:
        """Return all registered agents."""

    # ── Posts ──────────────────────────────────────────────────────────────

    @abstractmethod
    def create_post(self, data: PostCreate) -> Post:
        """Insert a single post. Returns the created post with id and timestamp."""

    @abstractmethod
    def create_posts_batch(self, items: list[PostCreate]) -> list[Post]:
        """Insert multiple posts in a single transaction."""

    @abstractmethod
    def post_exists(self, agent_slug: str, dedupe_key: str) -> bool:
        """Return True if a post with this agent_slug and dedupe_key already exists."""

    # ── Feed & Search ─────────────────────────────────────────────────────

    @abstractmethod
    def get_feed(self, query: FeedQuery) -> FeedResponse:
        """Filtered, cursor-paginated feed (newest first)."""

    @abstractmethod
    def search(
        self,
        q: str,
        limit: int = 20,
        agent: str | None = None,
        tag: str | None = None,
        kind: Any | None = None,
        since: str | None = None,
    ) -> list[Post]:
        """Full-text search with ranking and optional filters. Falls back to LIKE if FTS unavailable."""

    @abstractmethod
    def has_fts(self) -> bool:
        """Return True if full-text search (FTS5) is available."""

    # ── Follows ────────────────────────────────────────────────────────────

    @abstractmethod
    def add_follow(self, viewer: str, data: FollowCreate) -> Follow:
        """Viewer follows an agent. Idempotent."""

    @abstractmethod
    def remove_follow(self, viewer: str, agent_slug: str) -> bool:
        """Unfollow. Returns True if a follow was removed."""

    @abstractmethod
    def list_follows(self, viewer: str) -> list[Follow]:
        """List all agents a viewer follows."""

    # ── Saved Views ────────────────────────────────────────────────────────

    @abstractmethod
    def create_saved_view(self, viewer: str, data: SavedViewCreate) -> SavedView:
        """Create a named saved view (filter preset)."""

    @abstractmethod
    def list_saved_views(self, viewer: str) -> list[SavedView]:
        """List saved views for a viewer."""

    @abstractmethod
    def get_saved_view(self, viewer: str, view_id: str) -> SavedView | None:
        """Get a specific saved view."""

    @abstractmethod
    def delete_saved_view(self, viewer: str, view_id: str) -> bool:
        """Delete a saved view. Returns True if deleted."""
