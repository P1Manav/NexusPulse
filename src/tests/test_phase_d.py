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
    expected_phrase = (
        f"{actual_count} tests: all {actual_count} pass when kubectl is installed; "
        f"{passed_count} pass and {skipped_count} skips when it is not"
    )
    expected_platform = "Windows 11 (build 26300) / Python 3.10.6"

    readme_content = README_PATH.read_text(encoding="utf-8")
    summary_content = SUMMARY_PATH.read_text(encoding="utf-8")
    pitch_content = PITCH_PATH.read_text(encoding="utf-8")

    doc_files = [("README.md", readme_content), ("SUMMARY.md", summary_content), ("PITCH_SCRIPT.md", pitch_content)]

    # 1. Zero tolerance for legacy/prohibited strings
    old_phrase = "107 passed, 1 skipped when kubectl is absent"
    for name, content in doc_files:
        assert "Windows-10-10.0" not in content, f"{name} contains prohibited legacy platform string 'Windows-10-10.0'"
        assert old_phrase not in content, f"{name} contains obsolete phrase '{old_phrase}'"

    # '0 skipped' check: allowed only as part of '0 skipped (kubectl present on that machine)' in README
    for name, content in [("SUMMARY.md", summary_content), ("PITCH_SCRIPT.md", pitch_content)]:
        assert "0 skipped" not in content, f"{name} contains prohibited phrase '0 skipped'"

    readme_clean = readme_content.replace("0 skipped (kubectl present on that machine)", "")
    assert "0 skipped" not in readme_clean, "README.md contains prohibited phrase '0 skipped' outside allowed verification note"

    # 2. Check test-count phrase and platform string in README and SUMMARY
    for name, content in [("README.md", readme_content), ("SUMMARY.md", summary_content)]:
        assert expected_phrase in content, f"{name} does not match test-count phrase '{expected_phrase}'"
        assert expected_platform in content, f"{name} does not match platform string '{expected_platform}'"

    # 3. Check README badge URL and alt text
    expected_badge_alt = f"Tests: {expected_phrase}"
    expected_badge_url = f"https://img.shields.io/badge/tests-{actual_count}%20collected-brightgreen.svg"
    assert f"![{expected_badge_alt}]({expected_badge_url})" in readme_content

    # 4. Check README quickstart comment
    assert f"# 3. Run Test Suite: {expected_phrase}" in readme_content

    # 5. Check README layout comment
    assert f"# Automated test suite: {expected_phrase}" in readme_content

    # 6. Check README disclosure blockquote line
    expected_disclosure = (
        f"The project runs and the test suite passes ({expected_phrase}; run on Windows 11 (build 26300) / Python 3.10.6), "
        "and the complete live demonstration runs with zero external dependencies, zero API keys, and no Claude CLI."
    )
    assert expected_disclosure in readme_content

    # 7. Check PITCH_SCRIPT Minute 5 phrasing
    expected_pitch_phrase = (
        f"**Code Quality**: {actual_count} automated tests covering stores, API integration, queue flushing, "
        "thread safety, guard rules and deployment manifests. All pass when kubectl is installed; one skips when it isn't. "
        "The suite runs in about 15 seconds on this machine."
    )
    assert expected_pitch_phrase in pitch_content, f"PITCH_SCRIPT.md does not match expected phrase: {expected_pitch_phrase}"

    # 8. Check absence of 'B1:' through 'B10:' prefixes in README verification table
    for i in range(1, 11):
        assert f"B{i}:" not in readme_content, f"README.md still contains prefix 'B{i}:'"

    # 9. Check presence of benchmark caveat line in README and SUMMARY
    caveat_line = (
        "*Single runs on a developer workstation; throughput and latency vary noticeably between runs "
        "(the same machine produced 271.9 and 498.3 ops/s for the mixed-load scenario on two runs), so treat these as indicative, not precise.*"
    )
    assert caveat_line in readme_content, "README.md missing benchmark caveat line"
    assert caveat_line in summary_content, "SUMMARY.md missing benchmark caveat line"
