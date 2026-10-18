"""
board.store.sqlite — SQLite storage backend with FTS5 full-text search.

Features:
  - FTS5 virtual table for ranked full-text search (with LIKE fallback).
  - Keyset (cursor) pagination — never offset-based.
  - Thread-safe via a single re-entrant lock (RLock) guarding every public method.
  - Atomic batch writes with savepoints per item and rollback on failure.
  - Race-safe idempotency via dedupe_key and unique partial index.
  - JSON columns for tags and metadata.
  - Startup check for FTS5 availability.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
import uuid
from typing import Any

from board.feed import decode_cursor, encode_cursor, slugify
from board.models import (
    Agent,
    AgentRegister,
    FeedQuery,
    FeedResponse,
    Follow,
    FollowCreate,
    Post,
    PostCreate,
    SavedView,
    SavedViewCreate,
)
from board.store.base import Store

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class BatchResult(list):
    """Result of create_posts_batch containing inserted posts and count metadata."""

    def __init__(self, items: list[Post], inserted: int = 0, skipped: int = 0):
        super().__init__(items)
        self.inserted = inserted
        self.skipped = skipped
        self.inserted_count = inserted
        self.skipped_count = skipped


class SQLiteStore(Store):
    """SQLite-backed store with optional FTS5 and full thread safety."""

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._fts_available = False

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def init(self) -> None:
        with self._lock:
            self._conn = sqlite3.connect(
                self._db_path, check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_tables()
            self._try_create_fts()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def healthy(self) -> bool:
        with self._lock:
            if not self._conn:
                return False
            try:
                self._conn.execute("SELECT 1")
                return True
            except Exception:
                return False

    def has_fts(self) -> bool:
        with self._lock:
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
                dedupe_key  TEXT,
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

            CREATE TABLE IF NOT EXISTS post_tags (
                post_id     INTEGER NOT NULL,
                tag         TEXT NOT NULL,
                PRIMARY KEY (tag, post_id),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag, post_id);
        """)

        # Guarded migration for existing database files that lack dedupe_key column
        cur = self._conn.execute("PRAGMA table_info(posts)")
        cols = [r["name"] for r in cur.fetchall()]
        if "dedupe_key" not in cols:
            self._conn.execute("ALTER TABLE posts ADD COLUMN dedupe_key TEXT")

        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_agent_dedupe ON posts(agent_slug, dedupe_key)"
        )

        # Backfill post_tags if empty and posts exist
        try:
            count_row = self._conn.execute("SELECT COUNT(*) as count FROM post_tags").fetchone()
            if count_row and count_row["count"] == 0:
                post_rows = self._conn.execute("SELECT id, tags FROM posts").fetchall()
                for row in post_rows:
                    try:
                        t_list = json.loads(row["tags"])
                        if isinstance(t_list, list):
                            for t in t_list:
                                if isinstance(t, str) and t.strip():
                                    self._conn.execute(
                                        "INSERT OR IGNORE INTO post_tags (post_id, tag) VALUES (?, ?)",
                                        (row["id"], t.strip().lower()),
                                    )
                    except Exception:
                        pass
                self._conn.commit()
        except Exception:
            pass

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

    def _sync_post_tags(self, post_id: int, tags: list[str]) -> None:
        """Insert normalized tags into relational post_tags index."""
        if not self._conn:
            return
        for t in tags:
            if isinstance(t, str) and t.strip():
                self._conn.execute(
                    "INSERT OR IGNORE INTO post_tags (post_id, tag) VALUES (?, ?)",
                    (post_id, t.strip().lower()),
                )

    # ── Agents ─────────────────────────────────────────────────────────────

    def register_agent(self, data: AgentRegister) -> Agent:
        slug = slugify(data.name)
        now = _now_iso()
        tags_json = json.dumps(data.tags)

        with self._lock:
            assert self._conn is not None
            try:
                # Retrieve existing created_at if updating
                cur = self._conn.execute(
                    "SELECT created_at FROM agents WHERE slug = ?", (slug,)
                )
                existing = cur.fetchone()
                created_at = existing["created_at"] if existing else now

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
                    (slug, data.name, data.function, data.kind.value, tags_json, created_at, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return Agent(
            slug=slug, name=data.name, function=data.function,
            kind=data.kind, tags=data.tags,
            created_at=created_at, updated_at=now,
        )

    def get_agent(self, slug: str) -> Agent | None:
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM agents WHERE slug = ?", (slug,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_agent(row)

    def list_agents(self) -> list[Agent]:
        with self._lock:
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

    def post_exists(self, agent_slug: str, dedupe_key: str) -> bool:
        """Return True if a post with this agent_slug and dedupe_key already exists."""
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT 1 FROM posts WHERE agent_slug = ? AND dedupe_key = ? LIMIT 1",
                (agent_slug, dedupe_key),
            ).fetchone()
            return row is not None

    def create_post(self, data: PostCreate) -> Post:
        now = _now_iso()
        tags_json = json.dumps(data.tags)
        meta_json = json.dumps(data.metadata)

        with self._lock:
            assert self._conn is not None
            try:
                cur = self._conn.execute(
                    """INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, dedupe_key, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(agent_slug, dedupe_key) DO NOTHING""",
                    (data.agent_slug, data.text, data.kind.value,
                     tags_json, meta_json, data.source, data.dedupe_key, now),
                )
                if cur.rowcount > 0:
                    post_id = cur.lastrowid
                    assert post_id is not None
                    self._sync_fts(post_id, data.text, data.agent_slug, tags_json)
                    self._sync_post_tags(post_id, data.tags)
                    self._conn.commit()
                    return Post(
                        id=str(post_id), agent_slug=data.agent_slug, text=data.text,
                        kind=data.kind, tags=data.tags, metadata=data.metadata,
                        source=data.source, dedupe_key=data.dedupe_key, created_at=now,
                    )
                else:
                    self._conn.commit()
                    # Return existing post if dedupe conflict occurred
                    row = self._conn.execute(
                        "SELECT * FROM posts WHERE agent_slug = ? AND dedupe_key = ?",
                        (data.agent_slug, data.dedupe_key),
                    ).fetchone()
                    if row:
                        return self._row_to_post(row)
                    return Post(
                        id="-1", agent_slug=data.agent_slug, text=data.text,
                        kind=data.kind, tags=data.tags, metadata=data.metadata,
                        source=data.source, dedupe_key=data.dedupe_key, created_at=now,
                    )
            except Exception:
                self._conn.rollback()
                raise

    def create_posts_batch(self, items: list[PostCreate]) -> BatchResult:
        if not items:
            return BatchResult([], inserted=0, skipped=0)
        now = _now_iso()
        results: list[Post] = []
        inserted_count = 0
        skipped_count = 0

        with self._lock:
            assert self._conn is not None
            try:
                for idx, data in enumerate(items):
                    sp_name = f"sp_batch_{idx}"
                    self._conn.execute(f"SAVEPOINT {sp_name}")
                    try:
                        tags_json = json.dumps(data.tags)
                        meta_json = json.dumps(data.metadata)
                        cur = self._conn.execute(
                            """INSERT INTO posts (agent_slug, text, kind, tags, metadata, source, dedupe_key, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(agent_slug, dedupe_key) DO NOTHING""",
                            (data.agent_slug, data.text, data.kind.value,
                             tags_json, meta_json, data.source, data.dedupe_key, now),
                        )
                        if cur.rowcount > 0:
                            post_id = cur.lastrowid
                            assert post_id is not None
                            self._sync_fts(post_id, data.text, data.agent_slug, tags_json)
                            self._sync_post_tags(post_id, data.tags)
                            self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                            results.append(Post(
                                id=str(post_id), agent_slug=data.agent_slug, text=data.text,
                                kind=data.kind, tags=data.tags, metadata=data.metadata,
                                source=data.source, dedupe_key=data.dedupe_key, created_at=now,
                            ))
                            inserted_count += 1
                        else:
                            # Conflict / duplicate skipped
                            self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                            skipped_count += 1
                    except Exception as item_exc:
                        self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                        self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                        logger.error(
                            "Failed to write item %d in batch for agent '%s': %s",
                            idx, data.agent_slug, item_exc, exc_info=True,
                        )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return BatchResult(results, inserted=inserted_count, skipped=skipped_count)

    # ── Feed ──────────────────────────────────────────────────────────────

    def get_feed(self, query: FeedQuery | None = None) -> FeedResponse:
        with self._lock:
            assert self._conn is not None
            if query is None:
                query = FeedQuery()
            conditions: list[str] = []
            params: list[Any] = []

            # Followed agents filter
            if query.following and query.viewer:
                cur = self._conn.execute(
                    "SELECT agent_slug FROM follows WHERE viewer = ?", (query.viewer,)
                )
                followed_slugs = [r["agent_slug"] for r in cur.fetchall()]
                if not followed_slugs:
                    return FeedResponse(posts=[], next_cursor=None)
                placeholders = ",".join("?" for _ in followed_slugs)
                conditions.append(f"agent_slug IN ({placeholders})")
                params.extend(followed_slugs)

            # Filters
            if query.agent:
                conditions.append("agent_slug = ?")
                params.append(query.agent)
            if query.tag:
                conditions.append("id IN (SELECT post_id FROM post_tags WHERE tag = ?)")
                params.append(query.tag.strip().lower())
            if query.kind:
                kind_val = query.kind.value if hasattr(query.kind, "value") else str(query.kind)
                conditions.append("kind = ?")
                params.append(kind_val)
            if query.since:
                conditions.append("created_at >= ?")
                params.append(query.since)
            if query.q:
                if self._fts_available:
                    tokens = query.q.strip().split()
                    formatted = []
                    for t in tokens:
                        if t.upper() in {"OR", "AND", "NOT"}:
                            formatted.append(t.upper())
                        else:
                            clean = t.replace('"', '')
                            if clean:
                                formatted.append(f'"{clean}"')
                    fts_query = " ".join(formatted) if formatted else '""'
                    conditions.append("id IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?)")
                    params.append(fts_query)
                else:
                    conditions.append("(text LIKE ? OR agent_slug LIKE ?)")
                    params.extend([f"%{query.q}%", f"%{query.q}%"])

            # Keyset pagination: id < cursor, ORDER BY id DESC
            if query.cursor:
                cursor_id = decode_cursor(query.cursor)
                conditions.append("id < ?")
                params.append(cursor_id)

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            limit = min(query.limit, 100)
            params.append(limit + 1)  # fetch one extra to detect next page

            sql = f"SELECT * FROM posts {where} ORDER BY id DESC LIMIT ?"
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # If FTS matching failed in feed query, fall back to LIKE
                if query.q and self._fts_available:
                    fb_conditions = []
                    fb_params = []
                    for cond, p in zip(conditions, params[:-1]):
                        if "posts_fts MATCH" in cond:
                            fb_conditions.append("(text LIKE ? OR agent_slug LIKE ?)")
                            fb_params.extend([f"%{query.q}%", f"%{query.q}%"])
                        else:
                            fb_conditions.append(cond)
                            fb_params.append(p)
                    fb_where = "WHERE " + " AND ".join(fb_conditions) if fb_conditions else ""
                    fb_params.append(limit + 1)
                    rows = self._conn.execute(
                        f"SELECT * FROM posts {fb_where} ORDER BY id DESC LIMIT ?", fb_params
                    ).fetchall()
                else:
                    raise

            posts = [self._row_to_post(r) for r in rows[:limit]]
            next_cursor = None
            if len(rows) > limit:
                next_cursor = encode_cursor(int(posts[-1].id))

            return FeedResponse(posts=posts, next_cursor=next_cursor)

    # ── Search ────────────────────────────────────────────────────────────

    def search(
        self,
        q: str,
        limit: int = 20,
        agent: str | None = None,
        tag: str | None = None,
        kind: PostKind | None = None,
        since: str | None = None,
    ) -> list[Post]:
        with self._lock:
            assert self._conn is not None
            limit = min(limit, 100)

            if self._fts_available:
                return self._search_fts(q, limit, agent=agent, tag=tag, kind=kind, since=since)
            return self._search_like(q, limit, agent=agent, tag=tag, kind=kind, since=since)

    def _search_fts(
        self,
        q: str,
        limit: int,
        agent: str | None = None,
        tag: str | None = None,
        kind: PostKind | None = None,
        since: str | None = None,
    ) -> list[Post]:
        """Full-text search using FTS5 with BM25 ranking and optional filters."""
        assert self._conn is not None
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

        conditions = ["posts_fts MATCH ?"]
        params: list[Any] = [fts_query]

        if agent:
            conditions.append("posts.agent_slug = ?")
            params.append(agent)
        if tag:
            conditions.append("posts.id IN (SELECT post_id FROM post_tags WHERE tag = ?)")
            params.append(tag.strip().lower())
        if kind:
            kind_val = kind.value if hasattr(kind, "value") else str(kind)
            conditions.append("posts.kind = ?")
            params.append(kind_val)
        if since:
            conditions.append("posts.created_at >= ?")
            params.append(since)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        try:
            rows = self._conn.execute(
                f"""SELECT posts.* FROM posts_fts
                   JOIN posts ON posts.id = posts_fts.rowid
                   WHERE {where_clause}
                   ORDER BY rank
                   LIMIT ?""",
                params,
            ).fetchall()
            return [self._row_to_post(r) for r in rows]
        except sqlite3.OperationalError:
            return self._search_like(q, limit, agent=agent, tag=tag, kind=kind, since=since)

    def _search_like(
        self,
        q: str,
        limit: int,
        agent: str | None = None,
        tag: str | None = None,
        kind: PostKind | None = None,
        since: str | None = None,
    ) -> list[Post]:
        """Fallback search using LIKE (no ranking) with optional filters."""
        assert self._conn is not None
        conditions = ["(posts.text LIKE ? OR posts.agent_slug LIKE ? OR posts.tags LIKE ?)"]
        params: list[Any] = [f"%{q}%", f"%{q}%", f"%{q}%"]

        if agent:
            conditions.append("posts.agent_slug = ?")
            params.append(agent)
        if tag:
            conditions.append("posts.id IN (SELECT post_id FROM post_tags WHERE tag = ?)")
            params.append(tag.strip().lower())
        if kind:
            kind_val = kind.value if hasattr(kind, "value") else str(kind)
            conditions.append("posts.kind = ?")
            params.append(kind_val)
        if since:
            conditions.append("posts.created_at >= ?")
            params.append(since)

        where_clause = " AND ".join(conditions)
        params.append(limit)

        rows = self._conn.execute(
            f"""SELECT * FROM posts
               WHERE {where_clause}
               ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
        return [self._row_to_post(r) for r in rows]

    @staticmethod
    def _row_to_post(row: sqlite3.Row) -> Post:
        dedupe = row["dedupe_key"] if "dedupe_key" in row.keys() else None
        return Post(
            id=str(row["id"]),
            agent_slug=row["agent_slug"],
            text=row["text"],
            kind=row["kind"],
            tags=json.loads(row["tags"]),
            metadata=json.loads(row["metadata"]),
            source=row["source"],
            dedupe_key=dedupe,
            created_at=row["created_at"],
        )

    # ── Follows ────────────────────────────────────────────────────────────

    def add_follow(self, viewer: str, data: FollowCreate) -> Follow:
        now = _now_iso()
        with self._lock:
            assert self._conn is not None
            try:
                self._conn.execute(
                    """INSERT OR IGNORE INTO follows (viewer, agent_slug, created_at)
                       VALUES (?, ?, ?)""",
                    (viewer, data.agent_slug, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return Follow(viewer=viewer, agent_slug=data.agent_slug, created_at=now)

    def remove_follow(self, viewer: str, agent_slug: str) -> bool:
        with self._lock:
            assert self._conn is not None
            try:
                cur = self._conn.execute(
                    "DELETE FROM follows WHERE viewer = ? AND agent_slug = ?",
                    (viewer, agent_slug),
                )
                self._conn.commit()
                return cur.rowcount > 0
            except Exception:
                self._conn.rollback()
                raise

    def list_follows(self, viewer: str) -> list[Follow]:
        with self._lock:
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
            try:
                self._conn.execute(
                    """INSERT INTO saved_views (id, viewer, name, filters, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (view_id, viewer, data.name, filters_json, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        return SavedView(
            id=view_id, viewer=viewer, name=data.name,
            filters=data.filters, created_at=now,
        )

    def list_saved_views(self, viewer: str) -> list[SavedView]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM saved_views WHERE viewer = ? ORDER BY created_at DESC",
                (viewer,),
            ).fetchall()
            return [self._row_to_saved_view(r) for r in rows]

    def get_saved_view(self, viewer: str, view_id: str) -> SavedView | None:
        with self._lock:
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
            try:
                cur = self._conn.execute(
                    "DELETE FROM saved_views WHERE id = ? AND viewer = ?",
                    (view_id, viewer),
                )
                self._conn.commit()
                return cur.rowcount > 0
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _row_to_saved_view(row: sqlite3.Row) -> SavedView:
        return SavedView(
            id=row["id"],
            viewer=row["viewer"],
            name=row["name"],
            filters=json.loads(row["filters"]),
            created_at=row["created_at"],
        )
