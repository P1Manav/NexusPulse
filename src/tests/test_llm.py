"""
test_llm.py — Unit tests for LLM adapter.

Requirements tested:
  - Mock backend: deterministic scripted text.
  - Claude subprocess mocking:
      * Command construction (["claude", "-p", prompt])
      * Environment passthrough
      * Non-zero exit code -> LLMError
      * Timeout -> LLMError
      * Empty output -> LLMError
      * Missing binary -> LLMError
  - Auto backend selection logic (with/without PATH binary, with/without CLAUDE_KEY).
  - Budget tracker and budget exhaustion fallback.
  - Failure isolation (failed external call falls back to mock-fallback without crashing).
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from board import config
from board.llm import (
    LLMError,
    complete,
    complete_with_metadata,
    build_claude_command,
    is_claude_available,
    resolve_backend,
    BudgetTracker,
    reset_budget_tracker,
)


def test_mock_backend_deterministic():
    prompt = "Work item: Deploy Kubernetes ingress controllers"
    out = complete(prompt, backend="mock")
    assert "Completed work item: Deploy Kubernetes ingress controllers" in out
    assert "[verified]" in out

    resp = complete_with_metadata(prompt, backend="mock")
    assert resp.source == "mock"
    assert resp.failed is False


def test_claude_command_construction():
    cmd = build_claude_command("Hello world")
    assert cmd == ["claude", "-p", "Hello world"]


def test_auto_detection_logic():
    # Scenario A: Neither CLI nor key
    with patch("shutil.which", return_value=None), patch.dict(os.environ, {}, clear=True):
        assert is_claude_available() is False
        assert resolve_backend("auto") == "mock"

    # Scenario B: Binary exists, but CLAUDE_KEY missing
    with patch("shutil.which", return_value="/usr/local/bin/claude"), patch.dict(os.environ, {}, clear=True):
        assert is_claude_available() is False
        assert resolve_backend("auto") == "mock"

    # Scenario C: Binary exists AND CLAUDE_KEY is set
    with patch("shutil.which", return_value="/usr/local/bin/claude"), patch.dict(os.environ, {"CLAUDE_KEY": "sk-test-123"}):
        assert is_claude_available() is True
        assert resolve_backend("auto") == "claude"


def test_claude_success_mocked_subprocess():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Claude generated response for task."
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        resp = complete_with_metadata("Summarize project", backend="claude")
        assert resp.text == "Claude generated response for task."
        assert resp.source == "claude"
        assert resp.failed is False

        # Verify command and env passthrough
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == ["claude", "-p", "Summarize project"]
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert "env" in kwargs


def test_claude_nonzero_exit_code():
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "Authentication failed: invalid CLAUDE_KEY"

    with patch("subprocess.run", return_value=mock_proc):
        # With fallback_to_mock=False, raises LLMError
        with pytest.raises(LLMError) as exc_info:
            complete_with_metadata("Test prompt", backend="claude", fallback_to_mock=False)
        assert "Authentication failed" in str(exc_info.value)
        assert exc_info.value.category == "nonzero_exit"

        # With fallback_to_mock=True, falls back smoothly without raw errors
        resp = complete_with_metadata("Test prompt", backend="claude", fallback_to_mock=True)
        assert resp.source == "mock-fallback"
        assert resp.failed is True
        assert "[LLM unavailable: using fallback]" in resp.text
        assert "Authentication failed" not in resp.text
        assert resp.error_category == "nonzero_exit"


def test_claude_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=5)):
        resp = complete_with_metadata("Test prompt", backend="claude", fallback_to_mock=True)
        assert resp.source == "mock-fallback"
        assert resp.failed is True
        assert "[LLM unavailable: using fallback]" in resp.text
        assert resp.error_category == "timeout"


def test_claude_empty_output():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "   \n"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        resp = complete_with_metadata("Test prompt", backend="claude", fallback_to_mock=True)
        assert resp.source == "mock-fallback"
        assert resp.failed is True


def test_claude_missing_binary():
    with patch("subprocess.run", side_effect=FileNotFoundError("claude: not found")):
        resp = complete_with_metadata("Test prompt", backend="claude", fallback_to_mock=True)
        assert resp.source == "mock-fallback"
        assert resp.failed is True


def test_budget_exhaustion_fallback():
    # Setup tracker with only 0.1s budget
    tracker = reset_budget_tracker(budget_seconds=0.1)
    tracker.record(0.15)  # Exhaust budget

    with patch("board.llm.is_claude_available", return_value=True):
        # In auto mode, exhausted budget reverts to mock
        assert resolve_backend("auto") == "mock"

        # Explicit claude request with exhausted budget returns mock-fallback
        resp = complete_with_metadata("Test prompt", backend="claude")
        assert resp.source == "mock-fallback"

    # Reset tracker back to normal for other tests
    reset_budget_tracker(config.DEMO_LLM_BUDGET_SECONDS)
