"""
board.store.sqlite — SQLite storage backend with FTS5 full-text search.

Features:
  - FTS5 virtual table for ranked full-text search (with LIKE fallback).
  - Keyset (cursor) pagination — never offset-based.
  - Thread-safe via a single lock (fine for SQLite's single-writer model).
  - JSON columns for tags and metadata.
  - Startup check for FTS5 availability.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import datetime as dt
import uuid
from typing import Any

from board.models import (
    Agent, AgentRegister, Post, PostCreate, PostKind,
    FeedQuery, FeedResponse,
    Follow, FollowCreate, SavedView, SavedViewCreate,
)
from board.store.base import Store
from board.feed import encode_cursor, decode_cursor, slugify


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class SQLiteStore(Store):
    """SQLite-backed store with optional FTS5."""

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._fts_available = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def init(self) -> None:
        self._conn = sqlite3.connect(
            self._db_path, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._create_tables()
        self._try_create_fts()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def healthy(self) -> bool:
        if not self._conn:
            return False
        try:
            self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def has_fts(self) -> bool:
        return self._fts_available

    # ── Table Creation ─────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                slug        TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                function    TEXT NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'skill',
                tags        TEXT NOT NULL DEFAULT '[]',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_slug  TEXT NOT NULL,
                text        TEXT NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'update',
                tags        TEXT NOT NULL DEFAULT '[]',
                metadata    TEXT NOT NULL DEFAULT '{}',
                source      TEXT NOT NULL DEFAULT 'mock',
                created_at  TEXT NOT NULL,
                FOREIGN KEY (agent_slug) REFERENCES agents(slug)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_agent   ON posts(agent_slug);
            CREATE INDEX IF NOT EXISTS idx_posts_kind     ON posts(kind);
            CREATE INDEX IF NOT EXISTS idx_posts_created  ON posts(created_at);

            CREATE TABLE IF NOT EXISTS follows (
                viewer      TEXT NOT NULL,
                agent_slug  TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (viewer, agent_slug)
            );

            CREATE TABLE IF NOT EXISTS saved_views (
                id          TEXT PRIMARY KEY,
                viewer      TEXT NOT NULL,
                name        TEXT NOT NULL,
                filters     TEXT NOT NULL DEFAULT '{}',
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_saved_views_viewer ON saved_views(viewer);
        """)

    def _try_create_fts(self) -> None:
        """Try to create FTS5 virtual table. Graceful fallback if unavailable."""
        assert self._conn is not None
        try:
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts
                USING fts5(text, agent_slug, tags, content=posts, content_rowid=id)
            """)
            self._fts_available = True
        except sqlite3.OperationalError:
            self._fts_available = False

    def _sync_fts(self, post_id: int, text: str, agent_slug: str, tags: str) -> None:
        """Insert a row into the FTS index."""
        if not self._fts_available or not self._conn:
            return
        self._conn.execute(
            "INSERT INTO posts_fts(rowid, text, agent_slug, tags) VALUES (?, ?, ?, ?)",
            (post_id, text, agent_slug, tags),
        )

    # ── Agents ─────────────────────────────────────────────────────────────

    def register_agent(self, data: AgentRegister) -> Agent:
        slug = slugify(data.name)
        now = _now_iso()
        tags_json = json.dumps(data.tags)

        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """INSERT INTO agents (slug, name, function, kind, tags, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       name=excluded.name,
                       function=excluded.function,
                       kind=excluded.kind,
                       tags=excluded.tags,
                       updated_at=excluded.updated_at
                """,
                (slug, data.name, data.function, data.kind.value, tags_json, now, now),
            )
            self._conn.commit()

        return Agent(
            slug=slug, name=data.name, function=data.function,
            kind=data.kind, tags=data.tags,
            created_at=now, updated_at=now,
        )

    def get_agent(self, slug: str) -> Agent | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM agents WHERE slug = ?", (slug,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_agent(row)

    def list_agents(self) -> list[Agent]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM agents ORDER BY name"
        ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> Agent:
        return Agent(
            slug=row["slug"],
            name=row["name"],
            function=row["function"],
            kind=row["kind"],
            tags=json.loads(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Posts ──────────────────────────────────────────────────────────────

    def create_post(self, data: PostCreate) -> Post:
        now = _now_iso()
        tags_json = json.dumps(data.tags)
        meta_json = json.dumps(data.metadata)

        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                """INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (data.agent_slug, data.text, data.kind.value,
                 tags_json, meta_json, data.source, now),
            )
            post_id = cur.lastrowid
            assert post_id is not None
            self._sync_fts(post_id, data.text, data.agent_slug, tags_json)
            self._conn.commit()

        return Post(
            id=str(post_id), agent_slug=data.agent_slug, text=data.text,
            kind=data.kind, tags=data.tags, metadata=data.metadata,
            source=data.source, created_at=now,
        )

    def create_posts_batch(self, items: list[PostCreate]) -> list[Post]:
        if not items:
            return []
        now = _now_iso()
        results: list[Post] = []

        with self._lock:
            assert self._conn is not None
            for data in items:
                tags_json = json.dumps(data.tags)
                meta_json = json.dumps(data.metadata)
                cur = self._conn.execute(
                    """INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (data.agent_slug, data.text, data.kind.value,
                     tags_json, meta_json, data.source, now),
                )
                post_id = cur.lastrowid
                assert post_id is not None
                self._sync_fts(post_id, data.text, data.agent_slug, tags_json)
                results.append(Post(
                    id=str(post_id), agent_slug=data.agent_slug, text=data.text,
                    kind=data.kind, tags=data.tags, metadata=data.metadata,
                    source=data.source, created_at=now,
                ))
            self._conn.commit()

        return results

    # ── Feed ──────────────────────────────────────────────────────────────

    def get_feed(self, query: FeedQuery | None = None) -> FeedResponse:
        assert self._conn is not None
        if query is None:
            query = FeedQuery()
        conditions: list[str] = []
        params: list[Any] = []

        # Filters
        if query.agent:
            conditions.append("agent_slug = ?")
            params.append(query.agent)
        if query.tag:
            # Match tag inside JSON array string
            conditions.append("tags LIKE ?")
            params.append(f'%"{query.tag}"%')
        if query.kind:
            conditions.append("kind = ?")
            params.append(query.kind.value)
        if query.since:
            conditions.append("created_at >= ?")
            params.append(query.since)
        if query.q:
            # For feed, use simple LIKE search (FTS is via /search endpoint)
            conditions.append("text LIKE ?")
            params.append(f"%{query.q}%")

        # Cursor-based pagination (keyset): id < cursor, ORDER BY id DESC
        if query.cursor:
            cursor_id = decode_cursor(query.cursor)
            conditions.append("id < ?")
            params.append(cursor_id)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        limit = min(query.limit, 100)
        params.append(limit + 1)  # fetch one extra to detect next page

        sql = f"SELECT * FROM posts {where} ORDER BY id DESC LIMIT ?"
        rows = self._conn.execute(sql, params).fetchall()

        posts = [self._row_to_post(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            # There are more results
            next_cursor = encode_cursor(int(posts[-1].id))

        return FeedResponse(posts=posts, next_cursor=next_cursor)

    # ── Search ────────────────────────────────────────────────────────────

    def search(self, q: str, limit: int = 20) -> list[Post]:
        assert self._conn is not None
        limit = min(limit, 100)

        if self._fts_available:
            return self._search_fts(q, limit)
        return self._search_like(q, limit)

    def _search_fts(self, q: str, limit: int) -> list[Post]:
        """Full-text search using FTS5 with BM25 ranking."""
        assert self._conn is not None
        # Format tokens while preserving boolean operators (OR, AND, NOT)
        tokens = q.strip().split()
        formatted: list[str] = []
        for t in tokens:
            if t.upper() in {"OR", "AND", "NOT"}:
                formatted.append(t.upper())
            else:
                clean = t.replace('"', '')
                if clean:
                    formatted.append(f'"{clean}"')
        fts_query = " ".join(formatted) if formatted else '""'

        try:
            rows = self._conn.execute(
                """SELECT posts.* FROM posts_fts
                   JOIN posts ON posts.id = posts_fts.rowid
                   WHERE posts_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            return [self._row_to_post(r) for r in rows]
        except sqlite3.OperationalError:
            # Fall back to LIKE if query syntax was invalid
            return self._search_like(q, limit)

    def _search_like(self, q: str, limit: int) -> list[Post]:
        """Fallback search using LIKE (no ranking)."""
        assert self._conn is not None
        rows = self._conn.execute(
            """SELECT * FROM posts
               WHERE text LIKE ? OR agent_slug LIKE ? OR tags LIKE ?
               ORDER BY id DESC LIMIT ?""",
            (f"%{q}%", f"%{q}%", f"%{q}%", limit),
        ).fetchall()
        return [self._row_to_post(r) for r in rows]

    @staticmethod
    def _row_to_post(row: sqlite3.Row) -> Post:
        return Post(
            id=str(row["id"]),
            agent_slug=row["agent_slug"],
            text=row["text"],
            kind=row["kind"],
            tags=json.loads(row["tags"]),
            metadata=json.loads(row["metadata"]),
            source=row["source"],
            created_at=row["created_at"],
        )

    # ── Follows ────────────────────────────────────────────────────────────

    def add_follow(self, viewer: str, data: FollowCreate) -> Follow:
        now = _now_iso()
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """INSERT OR IGNORE INTO follows (viewer, agent_slug, created_at)
                   VALUES (?, ?, ?)""",
                (viewer, data.agent_slug, now),
            )
            self._conn.commit()
        return Follow(viewer=viewer, agent_slug=data.agent_slug, created_at=now)

    def remove_follow(self, viewer: str, agent_slug: str) -> bool:
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "DELETE FROM follows WHERE viewer = ? AND agent_slug = ?",
                (viewer, agent_slug),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_follows(self, viewer: str) -> list[Follow]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM follows WHERE viewer = ? ORDER BY created_at DESC",
            (viewer,),
        ).fetchall()
        return [
            Follow(viewer=r["viewer"], agent_slug=r["agent_slug"],
                   created_at=r["created_at"])
            for r in rows
        ]

    # ── Saved Views ────────────────────────────────────────────────────────

    def create_saved_view(self, viewer: str, data: SavedViewCreate) -> SavedView:
        view_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        filters_json = json.dumps(data.filters)

        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """INSERT INTO saved_views (id, viewer, name, filters, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (view_id, viewer, data.name, filters_json, now),
            )
            self._conn.commit()

        return SavedView(
            id=view_id, viewer=viewer, name=data.name,
            filters=data.filters, created_at=now,
        )

    def list_saved_views(self, viewer: str) -> list[SavedView]:
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM saved_views WHERE viewer = ? ORDER BY created_at DESC",
            (viewer,),
        ).fetchall()
        return [self._row_to_saved_view(r) for r in rows]

    def get_saved_view(self, viewer: str, view_id: str) -> SavedView | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT * FROM saved_views WHERE id = ? AND viewer = ?",
            (view_id, viewer),
        ).fetchone()
        if not row:
            return None
        return self._row_to_saved_view(row)

    def delete_saved_view(self, viewer: str, view_id: str) -> bool:
        with self._lock:
            assert self._conn is not None
            cur = self._conn.execute(
                "DELETE FROM saved_views WHERE id = ? AND viewer = ?",
                (view_id, viewer),
            )
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_saved_view(row: sqlite3.Row) -> SavedView:
        return SavedView(
            id=row["id"],
            viewer=row["viewer"],
            name=row["name"],
            filters=json.loads(row["filters"]),
            created_at=row["created_at"],
        )
