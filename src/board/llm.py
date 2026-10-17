"""
board.llm — Single LLM adapter interface.

Backends supported:
  - mock (default/fallback): deterministic scripted text, zero network/CLI dependencies.
  - claude: subprocess running 'claude -p', env passed unchanged, clear LLMError.
  - gemini: dev-only stub.
  - auto (default mode): uses claude if binary is on PATH and CLAUDE_KEY is set; else mock.

Budgeting:
  Tracks elapsed execution time against DEMO_LLM_BUDGET_SECONDS. Once budget is
  exhausted, automatically reverts to mock to guarantee deterministic termination.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from typing import NamedTuple

from board import config


class LLMError(Exception):
    """Raised when an LLM call fails (non-zero exit, timeout, missing binary, etc.)."""
    pass


class LLMResponse(NamedTuple):
    text: str
    source: str        # 'mock' | 'claude' | 'gemini' | 'mock-fallback'
    failed: bool       # True if primary provider failed and fell back to mock


# ── Budget Tracker ─────────────────────────────────────────────────────────

class BudgetTracker:
    """Thread-safe tracker for LLM invocation duration."""

    def __init__(self, budget_seconds: float):
        self.budget_seconds = budget_seconds
        self._spent_seconds = 0.0
        self._lock = threading.Lock()

    def record(self, duration: float) -> None:
        with self._lock:
            self._spent_seconds += duration

    def has_budget(self) -> bool:
        with self._lock:
            return self._spent_seconds < self.budget_seconds

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent_seconds


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
    """Construct command line arguments for Claude CLI.

    unverified against live CLI; confirm prompt-passing in official Claude Code docs.
    """
    return ["claude", "-p", prompt]


# ── Mock Implementation ────────────────────────────────────────────────────

def _mock_complete(prompt: str) -> str:
    """Deterministic scripted response derived from prompt contents."""
    cleaned = prompt.strip().replace("\r\n", " ").replace("\n", " ")
    # Produce clean, readable simulated update
    if "Work item:" in cleaned:
        item_part = cleaned.split("Work item:", 1)[1].strip()
        summary = item_part.split(".")[0].strip()
        return f"Completed work item: {summary} [verified]"
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


def _call_gemini(prompt: str) -> str:
    """Dev-only optional Gemini backend stub."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise LLMError("GEMINI_API_KEY is not configured")
    # Dev-only fallback
    return f"[Gemini Dev] Simulated response for: {prompt[:60]}"


# ── Main Adapter Entry Points ──────────────────────────────────────────────

def resolve_backend(requested: str | None = None) -> str:
    """Resolve backend choice (auto, claude, mock, gemini)."""
    target = (requested or os.getenv("LLM_BACKEND") or config.LLM_BACKEND).lower()
    if target == "auto":
        if is_claude_available() and _global_budget.has_budget():
            return "claude"
        return "mock"
    return target


def complete(prompt: str, backend: str | None = None) -> str:
    """Execute completion and return response string directly.

    If claude fails or budget is exhausted, falls back to mock.
    """
    resp = complete_with_metadata(prompt, backend=backend)
    return resp.text


def complete_with_metadata(
    prompt: str,
    backend: str | None = None,
    fallback_to_mock: bool = True,
) -> LLMResponse:
    """Execute completion and return LLMResponse with source and failure flags.

    Ensures failure isolation: a failure in an external LLM will not raise
    if fallback_to_mock is True, returning source='mock-fallback'.
    """
    selected = resolve_backend(backend)

    if selected == "mock":
        return LLMResponse(text=_mock_complete(prompt), source="mock", failed=False)

    if selected == "gemini":
        try:
            res = _call_gemini(prompt)
            return LLMResponse(text=res, source="gemini", failed=False)
        except Exception as exc:
            if not fallback_to_mock:
                raise
            return LLMResponse(
                text=f"{_mock_complete(prompt)} (gemini-fallback: {exc})",
                source="mock-fallback",
                failed=True,
            )

    if selected == "claude":
        # Check budget before firing
        if not _global_budget.has_budget():
            return LLMResponse(
                text=_mock_complete(prompt),
                source="mock-fallback",
                failed=False,
            )

        start = time.monotonic()
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
                text=f"{_mock_complete(prompt)} [fallback: {exc}]",
                source="mock-fallback",
                failed=True,
            )

    # Unknown backend -> default to mock
    return LLMResponse(text=_mock_complete(prompt), source="mock", failed=False)
