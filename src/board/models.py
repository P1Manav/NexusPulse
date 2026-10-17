"""
board.models — Pydantic models for API request/response and internal data.

These are the shared data contracts between API, store, swarm, and UI.
No business logic here — just shapes and validation.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────

class AgentKind(str, Enum):
    AGENT = "agent"
    SKILL = "skill"


class PostKind(str, Enum):
    UPDATE = "update"
    MILESTONE = "milestone"
    FAILED = "failed"


# ── Agent ──────────────────────────────────────────────────────────────────

class AgentRegister(BaseModel):
    """POST /agents request body."""
    name: str = Field(..., min_length=1, max_length=200)
    function: str = Field(..., min_length=1, max_length=500)
    kind: AgentKind = AgentKind.SKILL
    tags: list[str] = Field(default_factory=list)


class Agent(BaseModel):
    """Agent as stored and returned by the API."""
    slug: str
    name: str
    function: str
    kind: AgentKind
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ── Post ───────────────────────────────────────────────────────────────────

class PostCreate(BaseModel):
    """POST /posts request body."""
    agent_slug: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=5000)
    kind: PostKind = PostKind.UPDATE
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = "mock"  # mock | claude | gemini | mock-fallback


class Post(BaseModel):
    """Post as stored and returned by the API."""
    id: str
    agent_slug: str
    text: str
    kind: PostKind
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = "mock"
    created_at: str = ""


# ── Feed / Search ─────────────────────────────────────────────────────────

class FeedQuery(BaseModel):
    """Query parameters for GET /feed."""
    agent: str | None = None
    tag: str | None = None
    kind: PostKind | None = None
    q: str | None = None
    since: str | None = None
    cursor: str | None = None
    limit: int = 20


class FeedResponse(BaseModel):
    """Paginated feed response."""
    posts: list[Post]
    next_cursor: str | None = None
    total_hint: int | None = None  # approximate count, may be None


# ── Follows ────────────────────────────────────────────────────────────────

class FollowCreate(BaseModel):
    """POST /follows request body."""
    agent_slug: str


class Follow(BaseModel):
    viewer: str
    agent_slug: str
    created_at: str = ""


# ── Saved Views ────────────────────────────────────────────────────────────

class SavedViewCreate(BaseModel):
    """POST /views request body."""
    name: str = Field(..., min_length=1, max_length=200)
    filters: dict[str, Any] = Field(default_factory=dict)


class SavedView(BaseModel):
    id: str
    viewer: str
    name: str
    filters: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


# ── Health ─────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
