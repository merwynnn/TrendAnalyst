"""Shared pytest fixtures.

Rules (build brief §2, §7):
  * No live network in tests, ever. HTTP is exercised through recorded fixtures.
  * No secrets in fixtures. Recorded payloads are scrubbed before they are committed.
  * Tests that need Postgres carry the `db` marker; without a database, run
    `pytest -m "not db"`.

Database tests run against a SEPARATE database (`trend_analyst_test`), never the dev one:
that is what makes it safe for a test to migrate all the way up and back down again.
Both databases live on Neon (see USER_SETUP.md §1); the WSL scripts remain as fallback.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from config.settings import load_settings

TEST_DATABASE_NAME = "trend_analyst_test"
TEST_DB_URL_VAR = "TA_TEST_DB_URL"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def fixtures_dir(repo_root: Path) -> Path:
    """Directory holding recorded HTTP fixtures (secrets redacted)."""
    return repo_root / "tests" / "data"


def _test_database_url(repo_root: Path) -> str:
    """The test DSN: an explicit override, else the dev DSN pointed at the test database.

    NB: `str(URL)` renders the password as `***` (SQLAlchemy hides it on purpose), so the
    URL is rendered with `hide_password=False` here. Using `str()` would authenticate as
    a user whose password is literally `***`, which fails in a very confusing way.
    """
    override = os.environ.get(TEST_DB_URL_VAR)
    if override:
        return override

    settings = load_settings(repo_root / "config", env_name="dev")
    if not settings.db.configured:
        pytest.fail(
            "no database URL: paste the Neon DSN as db.url into "
            "config/secrets.local.yaml (USER_SETUP.md §1), or set TA_TEST_DB_URL. "
            "Use `pytest -m 'not db'` to run without a database."
        )
    url = make_url(settings.db.dsn).set(database=TEST_DATABASE_NAME)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def db_url(repo_root: Path) -> str:
    """DSN of the isolated test database, verified reachable before any test runs."""
    url = _test_database_url(repo_root)
    probe = create_engine(url, poolclass=None, connect_args={"connect_timeout": 5})
    try:
        with probe.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # any failure here means "not provisioned"
        pytest.fail(
            f"test database unreachable at {make_url(url).render_as_string(hide_password=True)} "
            f"({type(exc).__name__}: {exc}).\n"
            "Fix: check the Neon DSN in config/secrets.local.yaml (USER_SETUP.md §1)"
        )
    finally:
        probe.dispose()
    return url


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> Iterator[Engine]:
    """Engine against the test database, with the schema at head."""
    subprocess.run(
        # Run alembic with the CURRENT interpreter, not `uv run alembic`: a nested uv
        # invocation contends on uv's project lock with the `uv run pytest` that is
        # already holding it, and the suite hangs. sys.executable is this venv's python,
        # where alembic is installed.
        # `-x` is a GLOBAL alembic option: it must precede the subcommand.
        [sys.executable, "-m", "alembic", "-x", f"db_url={db_url}", "upgrade", "head"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    engine = create_engine(db_url, pool_pre_ping=True, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """A session inside a transaction that is always rolled back.

    Every test therefore starts from the same schema and leaves no rows behind, while
    still exercising real Postgres constraints and triggers.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
