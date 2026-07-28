"""Make the src layout importable for direct local pytest runs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--release-dist",
        type=Path,
        help="Use an existing wheel and sdist for release acceptance.",
    )
