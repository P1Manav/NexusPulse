"""
board.llm — Single LLM adapter interface.

Backends supported:
  - mock (default/fallback): deterministic scripted text, zero network/CLI dependencies.
  - claude: subprocess running 'claude -p', env passed unchanged, clear LLMError.
  - auto (default mode): uses claude if binary is on PATH and CLAUDE_KEY is set; else mock.

Budgeting:
  Tracks elapsed wall-clock execution time against DEMO_LLM_BUDGET_SECONDS. Once budget is
  exhausted, automatically reverts to mock to guarantee deterministic termination.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import NamedTuple

from board import config

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when an LLM call fails (non-zero exit, timeout, missing binary, etc.)."""
    pass


class LLMResponse(NamedTuple):
    text: str
    source: str        # 'mock' | 'claude' | 'mock-fallback'
    failed: bool       # True if primary provider failed and fell back to mock


# ── Budget Tracker ─────────────────────────────────────────────────────────

class BudgetTracker:
    """Thread-safe tracker for LLM wall-clock budget duration."""

    def __init__(self, budget_seconds: float):
        self.budget_seconds = budget_seconds
        self._start_time: float | None = None
        self._lock = threading.Lock()

    def start_if_not_started(self) -> None:
        with self._lock:
            if self._start_time is None:
                self._start_time = time.monotonic()

    def record(self, duration: float) -> None:
        with self._lock:
            if self._start_time is None:
                self._start_time = time.monotonic() - duration
            else:
                curr_elapsed = time.monotonic() - self._start_time
                if duration > curr_elapsed:
                    self._start_time = time.monotonic() - duration

    def has_budget(self) -> bool:
        with self._lock:
            if self._start_time is None:
                return True
            elapsed = time.monotonic() - self._start_time
            return elapsed < self.budget_seconds

    @property
    def spent(self) -> float:
        with self._lock:
            if self._start_time is None:
                return 0.0
            return max(0.0, time.monotonic() - self._start_time)


_global_budget = BudgetTracker(config.DEMO_LLM_BUDGET_SECONDS)


def get_budget_tracker() -> BudgetTracker:
    return _global_budget


def reset_budget_tracker(budget_seconds: float | None = None) -> BudgetTracker:
    global _global_budget
    _global_budget = BudgetTracker(budget_seconds or config.DEMO_LLM_BUDGET_SECONDS)
    return _global_budget


# ── Provider Checks & Commands ─────────────────────────────────────────────

def is_claude_available() -> bool:
    """Return True if 'claude' CLI binary is on PATH and CLAUDE_KEY is set in env."""
    has_binary = shutil.which("claude") is not None
    has_key = bool(os.getenv("CLAUDE_KEY", "").strip())
    return has_binary and has_key


def build_claude_command(prompt: str) -> list[str]:
    """Construct command line arguments for Claude CLI."""
    return ["claude", "-p", prompt]


# ── Mock Implementation ────────────────────────────────────────────────────

def _mock_complete(prompt: str, fallback_text: str | None = None) -> str:
    """Deterministic scripted response derived from prompt contents or explicit fallback."""
    if fallback_text:
        return fallback_text

    # Extract strictly the 'Work item:' line up to newline, without prompt instruction contamination
    for line in prompt.splitlines():
        line_clean = line.strip()
        if line_clean.startswith("Work item:"):
            item_text = line_clean[len("Work item:"):].strip()
            return f"Completed work item: {item_text} [verified]"

    cleaned = prompt.strip().replace("\r\n", " ").replace("\n", " ")
    if "Role:" in cleaned:
        role_part = cleaned.split("Role:", 1)[1].strip().split(".")[0]
        return f"Operational update from {role_part}: task executed successfully."
    return f"Processed task: {cleaned[:80]}... [success]"


# ── Execution Backends ─────────────────────────────────────────────────────

def _call_claude(prompt: str, timeout: int | None = None) -> str:
    """Call Claude CLI via subprocess."""
    cmd = build_claude_command(prompt)
    t = timeout or config.CLAUDE_TIMEOUT

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=t,
            env=os.environ.copy(),  # Environment passed through unchanged
        )
    except FileNotFoundError as exc:
        raise LLMError(f"Claude binary not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"Claude CLI timed out after {t}s") from exc
    except Exception as exc:
        raise LLMError(f"Failed to execute Claude subprocess: {exc}") from exc

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or f"process exited with code {proc.returncode}"
        raise LLMError(f"Claude CLI error: {err_msg}")

    output = proc.stdout.strip()
    if not output:
        raise LLMError("Claude CLI returned empty output")

    return output


# ── Main Adapter Entry Points ──────────────────────────────────────────────

def resolve_backend(requested: str | None = None) -> str:
    """Resolve backend choice (auto, claude, mock)."""
    target = (requested or os.getenv("LLM_BACKEND") or config.LLM_BACKEND).lower()
    if target == "auto":
        if is_claude_available() and _global_budget.has_budget():
            return "claude"
        return "mock"
    if target in ("claude", "mock"):
        return target
    logger.warning("Unrecognized LLM backend '%s'; defaulting to mock", target)
    return "mock"


def complete(prompt: str, backend: str | None = None, fallback_text: str | None = None) -> str:
    """Execute completion and return response string directly.

    If claude fails or budget is exhausted, falls back to mock.
    """
    resp = complete_with_metadata(prompt, backend=backend, fallback_text=fallback_text)
    return resp.text


def complete_with_metadata(
    prompt: str,
    backend: str | None = None,
    fallback_to_mock: bool = True,
    fallback_text: str | None = None,
) -> LLMResponse:
    """Execute completion and return LLMResponse with source and failure flags.

    Ensures failure isolation: a failure in an external LLM will not raise
    if fallback_to_mock is True, returning source='mock-fallback'.
    """
    selected = resolve_backend(backend)

    if selected == "mock":
        return LLMResponse(
            text=_mock_complete(prompt, fallback_text=fallback_text),
            source="mock",
            failed=False,
        )

    if selected == "claude":
        # Check budget before firing
        if not _global_budget.has_budget():
            return LLMResponse(
                text=_mock_complete(prompt, fallback_text=fallback_text),
                source="mock-fallback",
                failed=False,
            )

        start = time.monotonic()
        _global_budget.start_if_not_started()
        try:
            res = _call_claude(prompt)
            duration = time.monotonic() - start
            _global_budget.record(duration)
            return LLMResponse(text=res, source="claude", failed=False)
        except LLMError as exc:
            duration = time.monotonic() - start
            _global_budget.record(duration)
            if not fallback_to_mock:
                raise
            return LLMResponse(
                text=f"{_mock_complete(prompt, fallback_text=fallback_text)} [fallback: {exc}]",
                source="mock-fallback",
                failed=True,
            )

    return LLMResponse(
        text=_mock_complete(prompt, fallback_text=fallback_text),
        source="mock",
        failed=False,
    )
