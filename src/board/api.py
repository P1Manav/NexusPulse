"""
board.api — FastAPI application and server startup.

Stub for Phase M0. Full implementation in Phase M1.
"""
from __future__ import annotations

from fastapi import FastAPI
from board.config import HOST, PORT


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Agentic Message Board",
        version="0.1.0",
        description="LinkedIn-style message board for AI agents and skills",
    )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        return {"status": "ok"}

    return app


def run_server() -> None:
    """Start the uvicorn server."""
    import uvicorn
    app = create_app()
    uvicorn.run(app, host=HOST, port=PORT)
