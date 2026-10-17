"""
test_p3_ui_security.py — Verification suite for Phase P3 air-gapped UI and XSS defense.

Covers:
  - Zero external network requests (no Google Fonts, CDNs, or external URLs in HTML/CSS).
  - High-quality system font stacks used for air-gapped operation.
  - XSS defense: HTML escaping function present and used for all dynamic interpolations.
  - DOM event delegation: removal of vulnerable inline onclick string interpolations.
  - Dynamic viewer switcher: input with datalist and localStorage persistence.
"""
from __future__ import annotations

import re
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from board.api import create_app
from board.store.sqlite import SQLiteStore


@pytest.fixture
def html_content() -> str:
    ui_path = Path(__file__).resolve().parent.parent / "ui" / "index.html"
    return ui_path.read_text(encoding="utf-8")


def test_ui_zero_external_network_requests(html_content: str):
    """Ensure no external resources (Google Fonts, CDNs, http/https URLs) exist in UI."""
    assert "fonts.googleapis.com" not in html_content
    assert "fonts.gstatic.com" not in html_content
    
    # Check for any external http:// or https:// script/link/image/font src or href
    external_refs = re.findall(r'(?:src|href|url)\s*=\s*["\'](https?://[^"\']+)["\']', html_content, re.IGNORECASE)
    assert external_refs == [], f"Found external URL references in air-gapped UI: {external_refs}"


def test_ui_air_gapped_system_font_stacks(html_content: str):
    """Ensure high-quality system font stacks are declared instead of external webfonts."""
    assert "--font-main: -apple-system" in html_content
    assert "--font-mono: ui-monospace" in html_content


def test_ui_xss_escaping_and_event_delegation(html_content: str):
    """Ensure HTML escaping is defined and used, and inline onclick string interpolation is eliminated."""
    # esc function defined
    assert "function esc(str)" in html_content
    assert "&amp;" in html_content
    assert "&lt;" in html_content
    assert "&gt;" in html_content
    assert "&quot;" in html_content
    assert "&#39;" in html_content

    # Check that post text and tags are escaped
    assert "${esc(p.text)}" in html_content
    assert "${esc(t)}" in html_content
    assert "${esc(agent.name)}" in html_content
    assert "${esc(p.agent_slug)}" in html_content
    assert "${esc(p.kind)}" in html_content
    assert "${esc(p.source)}" in html_content

    # Ensure no vulnerable inline onclick handlers interpolate variables with quotes
    # e.g., onclick="filterByAgent('${...}')" or onclick="filterByTag('${...}')"
    vulnerable_onclicks = re.findall(r'onclick\s*=\s*["\'][^"\']*\$\{[^}]+\}[^"\']*["\']', html_content)
    assert vulnerable_onclicks == [], f"Found vulnerable inline onclicks with string interpolation: {vulnerable_onclicks}"

    # Event delegation listener is active
    assert "data-action" in html_content
    assert "initEventDelegation" in html_content


def test_ui_dynamic_viewer_switcher(html_content: str):
    """Ensure dynamic viewer switcher input with datalist replaces hardcoded select options."""
    assert 'id="viewerInput"' in html_content
    assert 'id="recentViewers"' in html_content
    assert 'datalist' in html_content
    assert "anonymous" in html_content
    assert "nexus_viewer" in html_content
    assert "nexus_recent_viewers" in html_content


def test_ui_served_via_api_endpoint():
    """Verify endpoint / returns the secured, air-gapped HTML."""
    store = SQLiteStore(":memory:")
    store.init()
    app = create_app(store=store)

    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "NexusPulse" in res.text
        assert "fonts.googleapis.com" not in res.text
        assert "esc(p.text)" in res.text

    store.close()
