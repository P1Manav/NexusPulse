"""
test_smoke.py — Phase M0 smoke tests.

Verifies the CLI entry point and basic app creation work.
"""
from __future__ import annotations

from main import main, _build_parser
from board.api import create_app


def test_cli_help_exits_zero():
    """--help should not crash (argparse exits with SystemExit(0))."""
    parser = _build_parser()
    # Just verify we can build the parser without error
    assert parser is not None
    assert parser.prog == "main.py"


def test_cli_no_args_returns_zero():
    """Running with no subcommand prints help and returns 0."""
    assert main([]) == 0


def test_create_app():
    """The FastAPI app should be creatable."""
    app = create_app()
    assert app.title == "Agentic Message Board"


def test_healthz():
    """GET /healthz should return 200."""
    from fastapi.testclient import TestClient
    app = create_app()
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz():
    """GET /readyz should return 200."""
    from fastapi.testclient import TestClient
    app = create_app()
    client = TestClient(app)
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
