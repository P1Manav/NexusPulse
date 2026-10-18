"""
test_phase_d.py — Hardening tests for Phase D and documentation test count honesty.

Covers:
  - Dynamically verifies collected tests count.
  - Verifies exact phrase: 'N tests: P passed, S skipped when kubectl is absent' across:
      1. README.md (badge alt text, quickstart comment, disclosure line, benchmark line, layout comment)
      2. docs/SUMMARY.md
      3. docs/PITCH_SCRIPT.md
  - Strictly asserts that none of these documentation files contains '0 skipped' or 'Windows-10-10.0'.
  - Asserts all three documentation files agree on the platform string.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

from board.config import SRC_DIR

REPO_ROOT = SRC_DIR.parent
README_PATH = REPO_ROOT / "README.md"
SUMMARY_PATH = REPO_ROOT / "docs" / "SUMMARY.md"
PITCH_PATH = REPO_ROOT / "docs" / "PITCH_SCRIPT.md"


class _TestCollector:
    def __init__(self):
        self.count = 0

    def pytest_collection_modifyitems(self, items):
        self.count = len(items)


def test_docs_test_count_no_drift():
    """Verify that the test count in README, SUMMARY, and PITCH_SCRIPT matches collected tests and never claims 0 skipped."""
    collector = _TestCollector()
    ret = pytest.main(["--collect-only", "-q", str(SRC_DIR / "tests")], plugins=[collector])
    assert ret == pytest.ExitCode.OK
    actual_count = collector.count
    assert actual_count > 0

    passed_count = actual_count - 1
    skipped_count = 1
    expected_phrase = f"{actual_count} tests: {passed_count} passed, {skipped_count} skipped when kubectl is absent"
    expected_platform = "Windows 11 (build 26300) / Python 3.10.6"

    readme_content = README_PATH.read_text(encoding="utf-8")
    summary_content = SUMMARY_PATH.read_text(encoding="utf-8")
    pitch_content = PITCH_PATH.read_text(encoding="utf-8")

    doc_files = [("README.md", readme_content), ("SUMMARY.md", summary_content), ("PITCH_SCRIPT.md", pitch_content)]

    # 1. Zero tolerance for '0 skipped' and 'Windows-10-10.0' in documentation files
    for name, content in doc_files:
        assert "0 skipped" not in content, f"{name} contains prohibited phrase '0 skipped'"
        assert "Windows-10-10.0" not in content, f"{name} contains prohibited legacy platform string 'Windows-10-10.0'"
        assert expected_phrase in content, f"{name} does not match test-count phrase '{expected_phrase}'"
        assert expected_platform in content, f"{name} does not match platform string '{expected_platform}'"

    # 2. Check README badge alt text and shield numbers
    assert f"![Tests: {expected_phrase}]" in readme_content
    badge_match = re.search(r"tests-(\d+)%20passed,%20(\d+)%20skipped", readme_content)
    assert badge_match is not None, "README.md missing test count badge with passed and skipped counts"
    assert int(badge_match.group(1)) == passed_count
    assert int(badge_match.group(2)) == skipped_count

    # 3. Check README quickstart comment
    assert f"# 3. Run Test Suite: {expected_phrase}" in readme_content

    # 4. Check README layout comment
    assert f"# Automated test suite: {expected_phrase}" in readme_content

    # 5. Check README disclosure line (exact phrasing, no nested parentheses)
    expected_disclosure = f"({expected_phrase}; run on Windows 11 / Python 3.10.6)"
    assert expected_disclosure in readme_content
    # Confirm no nested parentheses like ((108 tests...))
    assert f"(({expected_phrase}" not in readme_content
    assert f"({expected_phrase})" not in readme_content
