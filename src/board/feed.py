"""
board.feed — Feed helpers: cursor encoding, slug generation.

Cursor pagination uses base64-encoded post IDs so they are opaque
to clients (no guessing / manipulation).
"""
from __future__ import annotations

import base64
import re


def encode_cursor(post_id: int) -> str:
    """Encode an integer post ID as an opaque cursor string."""
    return base64.urlsafe_b64encode(str(post_id).encode()).decode()


def decode_cursor(cursor: str) -> int:
    """Decode an opaque cursor string back to a post ID."""
    return int(base64.urlsafe_b64decode(cursor.encode()).decode())


def slugify(name: str) -> str:
    """Convert an agent/skill name to a URL-safe slug.

    Example: "Frontend Designer Pro" -> "frontend-designer-pro"
    """
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s.strip("-")
