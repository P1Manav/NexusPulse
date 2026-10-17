"""
board.api — FastAPI application with all REST endpoints.

Endpoints:
  POST /agents              Register/upsert an agent (idempotent by slug).
  GET  /agents              List all agents.
  POST /posts               Create a post (goes through the batching queue).
  GET  /feed                Filtered, cursor-paginated feed.
  GET  /search              Full-text search with FTS5 ranking.
  POST /follows             Follow an agent (viewer from X-Viewer header).
  DELETE /follows/{slug}    Unfollow an agent.
  GET  /follows             List followed agents.
  POST /views               Create a saved view.
  GET  /views               List saved views.
  GET  /views/{id}/apply    Apply a saved view (returns feed with those filters).
  DELETE /views/{id}        Delete a saved view.
  GET  /healthz             Liveness probe.
  GET  /readyz              Readiness probe (checks store health).
  GET  /                    Serves the static UI.

Viewer identification: X-Viewer header (no auth). Defaults to "anonymous".
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from board import config
from board.models import (
    Agent, AgentRegister, Post, PostCreate, PostKind,
    FeedQuery, FeedResponse, HealthResponse,
    Follow, FollowCreate,
    SavedView, SavedViewCreate,
)
from board.store.sqlite import SQLiteStore
from board.queue import PostQueue


def _create_store() -> SQLiteStore:
    """Create and initialize the store based on config."""
    # Postgres support would go here behind a config check
    db_path = config.SQLITE_PATH
    store = SQLiteStore(db_path)
    store.init()
    return store


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure store + queue exist. Shutdown: drain queue, close store."""
    if not hasattr(app.state, "store") or app.state.store is None:
        app.state.store = _create_store()
    if not hasattr(app.state, "queue") or app.state.queue is None:
        app.state.queue = PostQueue(
            app.state.store,
            max_size=config.POST_QUEUE_MAX_SIZE,
            batch_size=config.POST_BATCH_SIZE,
            batch_interval_ms=config.POST_BATCH_INTERVAL_MS,
        )
    yield
    if hasattr(app.state, "queue") and app.state.queue is not None:
        app.state.queue.close()
    if hasattr(app.state, "store") and app.state.store is not None:
        app.state.store.close()


def create_app(store: SQLiteStore | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Agentic Message Board",
        version="0.1.0",
        description="LinkedIn-style message board for AI agents and skills",
        lifespan=lifespan,
    )

    if store is not None:
        app.state.store = store
        app.state.queue = PostQueue(
            store,
            max_size=config.POST_QUEUE_MAX_SIZE,
            batch_size=config.POST_BATCH_SIZE,
            batch_interval_ms=config.POST_BATCH_INTERVAL_MS,
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _store(request: Request) -> SQLiteStore:
        if not hasattr(request.app.state, "store") or request.app.state.store is None:
            request.app.state.store = _create_store()
        return request.app.state.store

    def _queue(request: Request) -> PostQueue:
        if not hasattr(request.app.state, "queue") or request.app.state.queue is None:
            s = _store(request)
            request.app.state.queue = PostQueue(
                s,
                max_size=config.POST_QUEUE_MAX_SIZE,
                batch_size=config.POST_BATCH_SIZE,
                batch_interval_ms=config.POST_BATCH_INTERVAL_MS,
            )
        return request.app.state.queue

    def _viewer(x_viewer: str | None = Header(default=None)) -> str:
        return x_viewer or "anonymous"

    # ── Health ─────────────────────────────────────────────────────────────

    @app.get("/healthz", response_model=HealthResponse)
    def healthz():
        return HealthResponse()

    @app.get("/readyz", response_model=HealthResponse)
    def readyz(request: Request):
        store = _store(request)
        if not store.healthy():
            raise HTTPException(503, "Store not healthy")
        return HealthResponse()

    # ── Agents ─────────────────────────────────────────────────────────────

    @app.post("/agents", response_model=Agent, status_code=201)
    def register_agent(data: AgentRegister, request: Request):
        return _store(request).register_agent(data)

    @app.get("/agents", response_model=list[Agent])
    def list_agents(request: Request):
        return _store(request).list_agents()

    # ── Posts ──────────────────────────────────────────────────────────────

    @app.post("/posts", response_model=dict, status_code=202)
    def create_post(data: PostCreate, request: Request):
        """Accepts a post into the queue. Returns 202 (Accepted)."""
        store = _store(request)
        # Verify agent exists
        if not store.get_agent(data.agent_slug):
            raise HTTPException(404, f"Agent '{data.agent_slug}' not found")
        _queue(request).put(data)
        return {"status": "accepted", "agent_slug": data.agent_slug}

    @app.post("/posts/sync", response_model=Post, status_code=201)
    def create_post_sync(data: PostCreate, request: Request):
        """Synchronous post creation (bypasses queue). Used by tests."""
        store = _store(request)
        if not store.get_agent(data.agent_slug):
            raise HTTPException(404, f"Agent '{data.agent_slug}' not found")
        return store.create_post(data)

    # ── Feed ──────────────────────────────────────────────────────────────

    @app.get("/feed", response_model=FeedResponse)
    def get_feed(
        request: Request,
        agent: str | None = Query(default=None),
        tag: str | None = Query(default=None),
        kind: PostKind | None = Query(default=None),
        q: str | None = Query(default=None),
        since: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=config.DEFAULT_PAGE_SIZE, le=config.MAX_PAGE_SIZE),
    ):
        query = FeedQuery(
            agent=agent, tag=tag, kind=kind, q=q,
            since=since, cursor=cursor, limit=limit,
        )
        return _store(request).get_feed(query)

    # ── Search ────────────────────────────────────────────────────────────

    @app.get("/search", response_model=list[Post])
    def search_posts(
        request: Request,
        q: str = Query(..., min_length=1),
        limit: int = Query(default=20, le=100),
    ):
        return _store(request).search(q, limit)

    # ── Follows ────────────────────────────────────────────────────────────

    @app.post("/follows", response_model=Follow, status_code=201)
    def follow_agent(
        data: FollowCreate, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _viewer(x_viewer)
        store = _store(request)
        if not store.get_agent(data.agent_slug):
            raise HTTPException(404, f"Agent '{data.agent_slug}' not found")
        return store.add_follow(viewer, data)

    @app.delete("/follows/{agent_slug}", status_code=200)
    def unfollow_agent(
        agent_slug: str, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _viewer(x_viewer)
        removed = _store(request).remove_follow(viewer, agent_slug)
        if not removed:
            raise HTTPException(404, "Follow not found")
        return {"status": "unfollowed", "agent_slug": agent_slug}

    @app.get("/follows", response_model=list[Follow])
    def list_follows(
        request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _viewer(x_viewer)
        return _store(request).list_follows(viewer)

    # ── Saved Views ────────────────────────────────────────────────────────

    @app.post("/views", response_model=SavedView, status_code=201)
    def create_saved_view(
        data: SavedViewCreate, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _viewer(x_viewer)
        return _store(request).create_saved_view(viewer, data)

    @app.get("/views", response_model=list[SavedView])
    def list_saved_views(
        request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _viewer(x_viewer)
        return _store(request).list_saved_views(viewer)

    @app.get("/views/{view_id}/apply", response_model=FeedResponse)
    def apply_saved_view(
        view_id: str, request: Request,
        x_viewer: str | None = Header(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=config.DEFAULT_PAGE_SIZE, le=config.MAX_PAGE_SIZE),
    ):
        viewer = _viewer(x_viewer)
        store = _store(request)
        view = store.get_saved_view(viewer, view_id)
        if not view:
            raise HTTPException(404, "Saved view not found")
        query = FeedQuery(
            agent=view.filters.get("agent"),
            tag=view.filters.get("tag"),
            kind=view.filters.get("kind"),
            q=view.filters.get("q"),
            since=view.filters.get("since"),
            cursor=cursor,
            limit=limit,
        )
        return store.get_feed(query)

    @app.delete("/views/{view_id}", status_code=200)
    def delete_saved_view(
        view_id: str, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _viewer(x_viewer)
        deleted = _store(request).delete_saved_view(viewer, view_id)
        if not deleted:
            raise HTTPException(404, "Saved view not found")
        return {"status": "deleted", "view_id": view_id}

    # ── Static UI ─────────────────────────────────────────────────────────

    ui_path = config.UI_DIR / "index.html"

    @app.get("/", response_class=HTMLResponse)
    def serve_ui():
        if ui_path.exists():
            return HTMLResponse(ui_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Agentic Message Board</h1><p>UI not found.</p>")

    return app


def run_server() -> None:
    """Start the uvicorn server."""
    import uvicorn
    app = create_app()
    uvicorn.run(app, host=config.HOST, port=config.PORT)
