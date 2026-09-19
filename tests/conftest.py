"""Shared pytest fixtures.

Rules (build brief §2, §7):
  * No live network in tests, ever. HTTP is exercised through recorded fixtures.
  * No secrets in fixtures. Recorded payloads are scrubbed before they are committed.
  * Tests that need Postgres carry the `db` marker; without a database, run
    `pytest -m "not db"`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def fixtures_dir(repo_root: Path) -> Path:
    """Directory holding recorded HTTP fixtures (secrets redacted)."""
    return repo_root / "tests" / "data"
