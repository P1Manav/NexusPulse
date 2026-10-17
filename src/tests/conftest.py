"""
conftest.py — shared pytest fixtures.

Adds src/ to sys.path so imports work when running `python -m pytest src/tests`.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is on the import path
SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
