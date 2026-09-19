"""Migration tests: the round trip, and the drift check that CI depends on.

`alembic check` is the load-bearing one: it fails the moment the models and the
migrations disagree, which is the only way to keep "migrations for every schema change"
(spec §11) honest.

These tests migrate the ISOLATED test database, never the development one. The round
trip always restores the schema to head, even if an assertion fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, text

from tests.test_store import EXPECTED_TABLES

pytestmark = pytest.mark.db


def alembic(repo_root: Path, *args: str, db_url: str) -> subprocess.CompletedProcess[str]:
    """Run an alembic command against the test database.

    `-x` is a global option, so it comes before the subcommand:
    ``alembic -x db_url=... upgrade head``.
    """
    return subprocess.run(
        # sys.executable, not `uv run`: see the note in conftest.py — a nested uv
        # invocation deadlocks against the outer `uv run pytest` on uv's project lock.
        [sys.executable, "-m", "alembic", "-x", f"db_url={db_url}", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def tables(db_engine: Engine) -> set[str]:
    return set(inspect(db_engine).get_table_names())


@pytest.fixture
def schema_at_head(repo_root: Path, db_url: str, db_engine: Engine) -> Iterator[Engine]:
    """Guarantee the schema is at head before and after a test that migrates."""
    alembic(repo_root, "upgrade", "head", db_url=db_url)
    try:
        yield db_engine
    finally:
        alembic(repo_root, "upgrade", "head", db_url=db_url)


def test_alembic_check_reports_no_drift(repo_root: Path, db_url: str) -> None:
    result = alembic(repo_root, "check", db_url=db_url)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "No new upgrade operations detected" in output


def test_migration_upgrade_then_downgrade_then_upgrade_again(
    repo_root: Path, db_url: str, schema_at_head: Engine
) -> None:
    assert tables(schema_at_head).issuperset(EXPECTED_TABLES)

    down = alembic(repo_root, "downgrade", "base", db_url=db_url)
    assert down.returncode == 0, down.stdout + down.stderr

    remaining = tables(schema_at_head)
    assert EXPECTED_TABLES & remaining == set(), f"tables survived the downgrade: {remaining}"
    assert "alembic_version" in remaining

    with schema_at_head.connect() as connection:
        views = connection.execute(
            text("SELECT viewname FROM pg_views WHERE viewname = 'quota_spend_daily'")
        ).all()
    assert views == [], "the daily quota view went with the tables"

    up = alembic(repo_root, "upgrade", "head", db_url=db_url)
    assert up.returncode == 0, up.stdout + up.stderr
    assert tables(schema_at_head).issuperset(EXPECTED_TABLES)


def test_downgrade_keeps_the_pgvector_extension(
    repo_root: Path, db_url: str, schema_at_head: Engine
) -> None:
    """Dropping the extension on downgrade would break every other user of the database."""
    down = alembic(repo_root, "downgrade", "base", db_url=db_url)
    assert down.returncode == 0, down.stdout + down.stderr

    with schema_at_head.connect() as connection:
        version = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    assert version is not None


def test_missing_database_url_is_reported_with_a_fix(tmp_path: Path) -> None:
    """The migration path must never silently fall back to a default database."""
    empty_config = tmp_path / "config"
    empty_config.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={
            **os.environ,
            "TA_CONFIG_DIR": str(empty_config),
            # A stray override in the developer's shell must not rescue this run.
            "TA_DB__URL": "",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_NO_MANAGED_PYTHON": "1",
        },
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "no database URL available for migrations" in output
