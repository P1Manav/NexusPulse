"""
board.feed — Feed helpers: cursor encoding, slug generation.

Cursor pagination uses base64-encoded post IDs so they are opaque
to clients (no guessing / manipulation).
"""
from __future__ import annotations

import base64
import re


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
