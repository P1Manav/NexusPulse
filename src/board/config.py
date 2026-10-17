"""
board.config — 12-factor configuration from environment variables.

Every tuneable is read from env. No config files, no CLI flags for config.
"""
from __future__ import annotations

import os
from pathlib import Path


# ── Paths ──────────────────────────────────────────────────────────────────
SRC_DIR = Path(__file__).resolve().parent.parent          # src/
AGENTS_DIR = SRC_DIR / "agents"                           # src/agents/
UI_DIR = SRC_DIR / "ui"                                   # src/ui/

# ── Server ─────────────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# ── Database ───────────────────────────────────────────────────────────────
# Store interface is abstract for future extensions; SQLite is the active single-replica store.
SQLITE_PATH: str = os.getenv("SQLITE_PATH", ":memory:")  # default in-memory

# ── LLM ────────────────────────────────────────────────────────────────────
LLM_BACKEND: str = os.getenv("LLM_BACKEND", "auto")      # auto|mock|claude
CLAUDE_TIMEOUT: int = int(os.getenv("CLAUDE_TIMEOUT", "60"))
DEMO_LLM_BUDGET_SECONDS: int = int(os.getenv("DEMO_LLM_BUDGET_SECONDS", "120"))
LLM_THREAD_POOL_SIZE: int = int(os.getenv("LLM_THREAD_POOL_SIZE", "4"))

# ── Queue ──────────────────────────────────────────────────────────────────
POST_QUEUE_MAX_SIZE: int = int(os.getenv("POST_QUEUE_MAX_SIZE", "1000"))
POST_BATCH_SIZE: int = int(os.getenv("POST_BATCH_SIZE", "50"))
POST_BATCH_INTERVAL_MS: int = int(os.getenv("POST_BATCH_INTERVAL_MS", "200"))
POST_QUEUE_PUT_TIMEOUT: float = float(os.getenv("POST_QUEUE_PUT_TIMEOUT", "0.2"))
ENABLE_SYNC_POSTS: bool = os.getenv("ENABLE_SYNC_POSTS", "0").lower() in ("1", "true", "yes")

# ── Pagination ─────────────────────────────────────────────────────────────
DEFAULT_PAGE_SIZE: int = int(os.getenv("DEFAULT_PAGE_SIZE", "20"))
MAX_PAGE_SIZE: int = int(os.getenv("MAX_PAGE_SIZE", "100"))

# ── API URL & Runner (for swarm runner to talk to the API) ───────────────
API_URL: str = os.getenv("API_URL", f"http://127.0.0.1:{PORT}")
SWARM_WAIT_SECONDS: int = int(os.getenv("SWARM_WAIT_SECONDS", "60"))

# ── Security & Authentication Guard ──────────────────────────────────────
BOARD_API_KEY: str | None = os.getenv("BOARD_API_KEY", "").strip() or None
