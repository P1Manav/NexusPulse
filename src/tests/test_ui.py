"""
test_ui.py — Verification that the single-page UI is served properly.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from board.api import create_app
from board.store.sqlite import SQLiteStore


def test_ui_served_with_correct_structure():
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text

        # Verify key brand and interactive components exist
        assert "NexusPulse" in body
        assert "Agentic Message Board" in body
        assert "searchInput" in body
        assert "viewerSelect" in body
        assert "postsList" in body
        assert "loadMoreBtn" in body
        assert "agentsDirectoryList" in body
        assert "savedViewsContainer" in body

    store.close()
