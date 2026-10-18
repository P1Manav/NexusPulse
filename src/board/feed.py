"""
board.feed — Feed helpers: cursor encoding, slug generation.

Cursor pagination uses base64-encoded post IDs so they are opaque
to clients (no guessing / manipulation).
"""
from __future__ import annotations

import base64
import datetime as dt
import re


def normalize_iso_timestamp(v: str | None) -> str | None:
    """Parse ISO-8601 timestamp string (Z, offsets, date-only, naive=UTC), convert to UTC ISO-8601."""
    if v is None:
        return None
    raw = v.strip()
    if not raw:
        return None
    if re.search(r"\s\d{2}:\d{2}$", raw):
        raw = re.sub(r"\s(\d{2}:\d{2})$", r"+\1", raw)
    raw = raw.replace("Z", "+00:00")
    try:
        if len(raw) == 10 and raw.count("-") == 2:
            d = dt.date.fromisoformat(raw)
            parsed = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)
        else:
            parsed = dt.datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            else:
                parsed = parsed.astimezone(dt.timezone.utc)
        return parsed.isoformat()
    except Exception as exc:
        raise ValueError(f"Invalid ISO-8601 timestamp: {exc}") from exc


def escape_like(query: str) -> str:
    """Escape %, _, and \\ for SQL LIKE queries using backslash as escape char."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor is malformed or invalid."""
    pass


def encode_cursor(post_id: int) -> str:
    """Encode an integer post ID as an opaque cursor string."""
    return base64.urlsafe_b64encode(str(post_id).encode()).decode()


def decode_cursor(cursor: str) -> int:
    """Decode an opaque cursor string back to an integer post ID."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        val = int(raw)
        if val <= 0:
            raise ValueError("Post ID must be positive")
        return val
    except Exception as exc:
        raise InvalidCursorError(f"Malformed pagination cursor: {cursor}") from exc


def slugify(name: str) -> str:
    """Convert an agent/skill name to a URL-safe slug.

    Example: "Frontend Designer Pro" -> "frontend-designer-pro"
    """
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s.strip("-")
