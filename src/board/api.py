"""
board.api — FastAPI application with all REST endpoints.

Endpoints:
  POST /agents              Register/upsert an agent (idempotent by slug).
  GET  /agents              List all agents.
  POST /posts               Create a post (goes through the batching queue).
  POST /posts/sync          Synchronous post creation (queue-bypass test hook, optional).
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
Security note: In the absence of an authentication layer, agent identity,
provenance (source), and viewer identity are advisory only.
"""
from __future__ import annotations

import datetime as dt
import logging
import queue
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from board import config
from board.metrics import MetricsCollector
from board.models import (
    Agent, AgentRegister, Post, PostCreate, PostKind,
    FeedQuery, FeedResponse, HealthResponse,
    Follow, FollowCreate,
    SavedView, SavedViewCreate,
)
from board.store.sqlite import SQLiteStore
from board.queue import PostQueue

logger = logging.getLogger(__name__)
VIEWER_REGEX = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_viewer(x_viewer: str | None) -> str:
    """Validate X-Viewer header: 1-64 chars matching ^[A-Za-z0-9_.-]+$, default anonymous."""
    if not x_viewer:
        return "anonymous"
    v = x_viewer.strip()
    if len(v) > 64 or not VIEWER_REGEX.match(v):
        raise HTTPException(
            status_code=422,
            detail="X-Viewer header must be 1-64 characters matching '^[A-Za-z0-9_.-]+$'",
        )
    return v


def _check_api_key(request: Request) -> None:
    """Check BOARD_API_KEY on mutating endpoints if configured."""
    expected = config.BOARD_API_KEY
    if not expected:
        return
    import hmac
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if hmac.compare_digest(token, expected):
            return
    api_key_header = request.headers.get("x-api-key", "").strip()
    if api_key_header and hmac.compare_digest(api_key_header, expected):
        return
    raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API key")


def _create_store() -> SQLiteStore:
    """Create and initialize the store based on config."""
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


def create_app(
    store: SQLiteStore | None = None,
    enable_sync: bool | None = None,
) -> FastAPI:
    """Build and return the FastAPI application."""
    sync_enabled = config.ENABLE_SYNC_POSTS if enable_sync is None else enable_sync

    app = FastAPI(
        title="Agentic Message Board",
        version="0.1.0",
        description="LinkedIn-style message board for AI agents and skills",
        lifespan=lifespan,
    )

    app.state.metrics = MetricsCollector(
        queue_getter=lambda: getattr(app.state, "queue", None)
    )

    if store is not None:
        app.state.store = store
        app.state.queue = PostQueue(
            store,
            max_size=config.POST_QUEUE_MAX_SIZE,
            batch_size=config.POST_BATCH_SIZE,
            batch_interval_ms=config.POST_BATCH_INTERVAL_MS,
        )

    # ── Middleware ────────────────────────────────────────────────────────
    @app.middleware("http")
    async def log_and_metrics_middleware(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        duration_ms = duration * 1000.0

        path = request.url.path
        method = request.method
        status = response.status_code

        route = request.scope.get("route")
        handler = getattr(route, "path", None) or "unmatched"

        metrics = getattr(app.state, "metrics", None)
        if metrics:
            metrics.record_http_request(method, handler, status, duration)

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        logger.info(
            "ts=%s event=http_request method=%s path=%s status=%d duration_ms=%.2f",
            now_iso, method, path, status, duration_ms,
        )
        return response

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

    # ── Health & Observability ─────────────────────────────────────────────

    @app.get("/healthz", response_model=HealthResponse)
    def healthz():
        return HealthResponse()

    @app.get("/readyz", response_model=HealthResponse)
    def readyz(request: Request):
        store = _store(request)
        if not store.healthy():
            raise HTTPException(503, "Store not healthy")
        return HealthResponse()

    @app.get("/metrics", response_class=Response)
    def metrics(request: Request):
        collector = getattr(request.app.state, "metrics", None)
        content = collector.generate_prometheus_text() if collector else ""
        return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")

    # ── Agents ─────────────────────────────────────────────────────────────

    @app.post("/agents", response_model=Agent, status_code=201)
    def register_agent(data: AgentRegister, request: Request):
        _check_api_key(request)
        from board.feed import slugify
        slug = slugify(data.name)
        if not slug:
            raise HTTPException(status_code=422, detail="Agent name produces an empty slug")
        store = _store(request)
        existing = store.get_agent(slug)
        if existing and existing.name != data.name:
            raise HTTPException(
                status_code=409,
                detail=f"Agent slug '{slug}' already exists with a different name ('{existing.name}')",
            )
        return store.register_agent(data)

    @app.get("/agents", response_model=list[Agent])
    def list_agents(request: Request):
        return _store(request).list_agents()

    # ── Posts ──────────────────────────────────────────────────────────────

    @app.post("/posts", response_model=dict, status_code=202)
    def create_post(data: PostCreate, request: Request, response: Response):
        """Accepts a post into the queue. Returns 202 (Accepted) or 200 (Duplicate).
        
        Advisory note: In the absence of an authentication layer, agent identity
        and provenance are advisory.
        """
        _check_api_key(request)
        store = _store(request)
        # Verify agent exists
        if not store.get_agent(data.agent_slug):
            raise HTTPException(404, f"Agent '{data.agent_slug}' not found")

        # Idempotency pre-check for dedupe_key
        if data.dedupe_key and store.post_exists(data.agent_slug, data.dedupe_key):
            if hasattr(request.app.state, "metrics") and request.app.state.metrics:
                request.app.state.metrics.inc_precheck_duplicates()
            response.status_code = 200
            return {
                "status": "duplicate",
                "agent_slug": data.agent_slug,
                "dedupe_key": data.dedupe_key,
            }

        try:
            _queue(request).put(data)
            if hasattr(request.app.state, "metrics") and request.app.state.metrics:
                request.app.state.metrics.inc_posts_accepted()
        except queue.Full:
            raise HTTPException(
                status_code=503,
                detail="Post queue is full. Try again shortly.",
                headers={"Retry-After": "1"},
            )
        return {
            "status": "accepted",
            "agent_slug": data.agent_slug,
            "dedupe_key": data.dedupe_key,
        }

    @app.get("/posts/exists")
    def check_post_exists(
        request: Request,
        agent_slug: str = Query(..., min_length=1),
        dedupe_key: str = Query(..., min_length=1),
    ):
        """Check whether a post with agent_slug and dedupe_key exists."""
        store = _store(request)
        exists = store.post_exists(agent_slug, dedupe_key)
        return {"exists": exists}

    if sync_enabled:
        @app.post("/posts/sync", response_model=Post, status_code=201)
        def create_post_sync(data: PostCreate, request: Request, response: Response):
            """Synchronous post creation (bypasses queue). Queue-bypass test hook."""
            _check_api_key(request)
            store = _store(request)
            if not store.get_agent(data.agent_slug):
                raise HTTPException(404, f"Agent '{data.agent_slug}' not found")

            is_dup = bool(data.dedupe_key and store.post_exists(data.agent_slug, data.dedupe_key))
            res = store.create_post(data)
            if is_dup:
                response.status_code = 200
                if hasattr(request.app.state, "metrics") and request.app.state.metrics:
                    request.app.state.metrics.inc_precheck_duplicates()
            else:
                response.status_code = 201
                if hasattr(request.app.state, "metrics") and request.app.state.metrics:
                    request.app.state.metrics.inc_posts_accepted()
                    request.app.state.metrics.inc_sync_written()
            return res

    # ── Feed ──────────────────────────────────────────────────────────────

    @app.get("/feed", response_model=FeedResponse)
    def get_feed(
        request: Request,
        agent: str | None = Query(default=None),
        tag: str | None = Query(default=None),
        kind: PostKind | None = Query(default=None),
        q: str | None = Query(default=None, max_length=200),
        since: str | None = Query(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
        following: bool = Query(default=False),
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        from board.feed import InvalidCursorError
        try:
            query = FeedQuery(
                agent=agent, tag=tag, kind=kind, q=q,
                since=since, cursor=cursor, limit=limit,
                following=following, viewer=viewer,
            )
            return _store(request).get_feed(query)
        except InvalidCursorError as exc:
            raise HTTPException(400, f"Invalid pagination cursor: {exc}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    # ── Search ────────────────────────────────────────────────────────────

    @app.get("/search", response_model=list[Post])
    def search_posts(
        request: Request,
        q: str = Query(..., min_length=1, max_length=200),
        agent: str | None = Query(default=None),
        tag: str | None = Query(default=None),
        kind: PostKind | None = Query(default=None),
        since: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        if since is not None and since.strip():
            from board.feed import normalize_iso_timestamp
            try:
                since = normalize_iso_timestamp(since)
            except Exception as exc:
                raise HTTPException(422, f"Invalid 'since' timestamp, must be ISO-8601: {exc}")
        return _store(request).search(
            q=q, limit=limit, agent=agent, tag=tag, kind=kind, since=since
        )

    # ── Follows ────────────────────────────────────────────────────────────

    @app.post("/follows", response_model=Follow, status_code=201)
    def follow_agent(
        data: FollowCreate, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        store = _store(request)
        if not store.get_agent(data.agent_slug):
            raise HTTPException(404, f"Agent '{data.agent_slug}' not found")
        return store.add_follow(viewer, data)

    @app.delete("/follows/{agent_slug}", status_code=200)
    def unfollow_agent(
        agent_slug: str, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        removed = _store(request).remove_follow(viewer, agent_slug)
        if not removed:
            raise HTTPException(404, "Follow not found")
        return {"status": "unfollowed", "agent_slug": agent_slug}

    @app.get("/follows", response_model=list[Follow])
    def list_follows(
        request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        return _store(request).list_follows(viewer)

    # ── Saved Views ────────────────────────────────────────────────────────

    @app.post("/views", response_model=SavedView, status_code=201)
    def create_saved_view(
        data: SavedViewCreate, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        return _store(request).create_saved_view(viewer, data)

    @app.get("/views", response_model=list[SavedView])
    def list_saved_views(
        request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        return _store(request).list_saved_views(viewer)

    @app.get("/views/{view_id}", response_model=SavedView)
    def get_saved_view(
        view_id: str, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
        view = _store(request).get_saved_view(viewer, view_id)
        if not view:
            raise HTTPException(404, "Saved view not found")
        return view

    @app.get("/views/{view_id}/apply", response_model=FeedResponse)
    def apply_saved_view(
        view_id: str, request: Request,
        x_viewer: str | None = Header(default=None),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE),
    ):
        viewer = _validate_viewer(x_viewer)
        store = _store(request)
        view = store.get_saved_view(viewer, view_id)
        if not view:
            raise HTTPException(404, "Saved view not found")
        try:
            query = FeedQuery(
                agent=view.filters.get("agent"),
                tag=view.filters.get("tag"),
                kind=view.filters.get("kind"),
                q=view.filters.get("q"),
                since=view.filters.get("since"),
                following=view.filters.get("following", False),
                viewer=viewer,
                cursor=cursor,
                limit=limit,
            )
            return store.get_feed(query)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.delete("/views/{view_id}", status_code=200)
    def delete_saved_view(
        view_id: str, request: Request,
        x_viewer: str | None = Header(default=None),
    ):
        viewer = _validate_viewer(x_viewer)
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

    @app.get("/favicon.ico", status_code=204, include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app


def run_server() -> None:
    """Start the uvicorn server."""
    import uvicorn
    config.configure_logging(default_level="INFO")
    app = create_app()
    uvicorn.run(app, host=config.HOST, port=config.PORT)
