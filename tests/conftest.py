"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def session_fixture() -> Path:
    return FIXTURES / "session.jsonl"
