"""
test_phase_d.py — Hardening tests for Phase D.

D2: Test count drift prevention guard. Fails if README test counts or the
badge number disagree with the real collected count (run in-process via pytest).
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

from board.config import SRC_DIR

REPO_ROOT = SRC_DIR.parent
README_PATH = REPO_ROOT / "README.md"


class _TestCollector:
    def __init__(self):
        self.count = 0

    def pytest_collection_modifyitems(self, items):
        self.count = len(items)


def test_docs_test_count_no_drift():
    """Verify that the test count in README badge and text matches collected tests exactly."""
    collector = _TestCollector()
    ret = pytest.main(["--collect-only", "-q", str(SRC_DIR / "tests")], plugins=[collector])
    assert ret == pytest.ExitCode.OK
    actual_count = collector.count
    assert actual_count > 0

    readme_content = README_PATH.read_text(encoding="utf-8")

    # Match badge: ![Tests: 100 Passed](https://img.shields.io/badge/tests-100%20passed-brightgreen.svg)
    badge_match = re.search(r"tests-(\d+)%20passed", readme_content)
    assert badge_match is not None, "README.md missing test count badge"
    badge_count = int(badge_match.group(1))
    assert badge_count == actual_count, (
        f"README badge test count ({badge_count}) does not match real collected tests ({actual_count})"
    )

    # Match Quickstart text: # 3. Run Test Suite (100 passing tests)
    quickstart_match = re.search(r"# 3\. Run Test Suite \((\d+) passing tests\)", readme_content)
    assert quickstart_match is not None, "README.md missing quickstart test count text"
    quickstart_count = int(quickstart_match.group(1))
    assert quickstart_count == actual_count, (
        f"README quickstart test count ({quickstart_count}) does not match real collected tests ({actual_count})"
    )

    # Match disclosure text: passes all 100 tests
    disclosure_match = re.search(r"passes all (\d+) tests", readme_content)
    assert disclosure_match is not None, "README.md missing disclosure line test count"
    disclosure_count = int(disclosure_match.group(1))
    assert disclosure_count == actual_count, (
        f"README disclosure test count ({disclosure_count}) does not match real collected tests ({actual_count})"
    )
