"""
board.models — Pydantic models for API request/response and internal data.

These are the shared data contracts between API, store, swarm, and UI.
No business logic here — just shapes and validation.
"""
from __future__ import annotations

import datetime as dt
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _validate_tags_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        raise ValueError("tags must be a list of strings")
    if len(v) > 20:
        raise ValueError("tags list cannot exceed 20 items")
    cleaned: list[str] = []
    for item in v:
        if not isinstance(item, str):
            raise ValueError("each tag must be a string")
        norm = item.strip().lower()
        if len(norm) > 40:
            raise ValueError(f"tag exceeds maximum length of 40 characters: '{norm}'")
        if norm:
            cleaned.append(norm)
    return cleaned


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

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v: Any) -> list[str]:
        return _validate_tags_list(v)


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
    """POST /posts request body.
    
    Security note: Without an authentication layer, agent_slug and source identity
    are advisory only.
    """
    agent_slug: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=5000)
    kind: PostKind = PostKind.UPDATE
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(default="api", pattern=r"^(mock|claude|mock-fallback|api)$")
    dedupe_key: str | None = Field(default=None, max_length=128)

    @field_validator("tags", mode="before")
    @classmethod
    def validate_tags(cls, v: Any) -> list[str]:
        return _validate_tags_list(v)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("metadata must be a dictionary")
        try:
            serialized = json.dumps(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"metadata must be JSON-serializable: {exc}")
        if len(serialized.encode("utf-8")) > 8192:
            raise ValueError("metadata exceeds maximum serialized size of 8 KB (8192 bytes)")
        return v


class Post(BaseModel):
    """Post as stored and returned by the API."""
    id: str
    agent_slug: str
    text: str
    kind: PostKind
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = "mock"
    dedupe_key: str | None = None
    created_at: str = ""


# ── Feed / Search ─────────────────────────────────────────────────────────

class FeedQuery(BaseModel):
    """Query parameters for GET /feed."""
    agent: str | None = None
    tag: str | None = None
    kind: PostKind | None = None
    q: str | None = Field(default=None, max_length=200)
    since: str | None = None
    cursor: str | None = None
    limit: int = Field(default=20, ge=1)
    following: bool = False
    viewer: str | None = None

    @field_validator("since")
    @classmethod
    def validate_since(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            from board.feed import normalize_iso_timestamp
            try:
                return normalize_iso_timestamp(v)
            except Exception as exc:
                raise ValueError(f"Invalid 'since' timestamp, must be ISO-8601: {exc}")
        return v


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
