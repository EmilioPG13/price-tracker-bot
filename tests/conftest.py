"""Shared test plumbing.

Every test in this suite runs offline. The store pages under `tests/fixtures/` are real
captures, so the parser is tested against what Cyberpuerta actually served rather than
against markup written to make the parser pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture():
    """Read a saved store page, byte-exact.

    `.gitattributes` marks `tests/fixtures/**` as `-text` so git never rewrites a line
    ending inside one; the whole value of a capture is that it has not been touched.
    """

    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load
