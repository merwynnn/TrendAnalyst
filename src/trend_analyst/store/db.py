"""Engine and session factories, built from the layered settings.

Kept deliberately free of pipeline logic: this module answers "how do I talk to
Postgres", and :func:`check_database` answers "is it there, and does it have pgvector" —
which is what the health CLI reports (spec §9).

The DSN comes from the layered settings (so: from `config/secrets.local.yaml`, never from
the repository). A missing DSN is a typed error with the command that fixes it, not a
connection attempt against a default.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from config.settings import Settings

__all__ = [
    "DatabaseNotConfiguredError",
    "DatabaseStatus",
    "check_database",
    "create_db_engine",
    "create_session_factory",
    "session_scope",
]


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when no database URL is available. Says how to fix it."""


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """What the health CLI needs to know about the database (spec §9)."""

    ok: bool
    server_version: str | None = None
    pgvector_version: str | None = None
    detail: str | None = None


def create_db_engine(
    settings: Settings | None = None,
    *,
    url: str | None = None,
    echo: bool | None = None,
) -> Engine:
    """Build an engine.

    Args:
        settings: layered settings; ``settings.db`` supplies the DSN and pool options.
        url: an explicit DSN, suppressing the settings lookup (used by tests and CI).
        echo: override statement logging.

    Raises:
        DatabaseNotConfiguredError: no DSN was found.
    """
    if url is None:
        if settings is None:
            # Imported here on purpose: the store package stays importable without the
            # config package, and only a caller that wants the layered settings pulls it in.
            from config.settings import load_settings  # noqa: PLC0415

            settings = load_settings()
        if not settings.db.configured:
            raise DatabaseNotConfiguredError(
                "db.url is empty. Run `bash scripts/provision_pg.sh` (it writes the DSN "
                "into config/secrets.local.yaml), or pass an explicit url."
            )
        url = settings.db.dsn

    pool_size = settings.db.pool_size if settings is not None else 5
    connect_timeout = settings.db.connect_timeout_s if settings is not None else 10
    statement_timeout_ms = settings.db.statement_timeout_ms if settings is not None else 30_000

    return create_engine(
        url,
        echo=settings.db.echo_sql if (settings is not None and echo is None) else bool(echo),
        pool_size=pool_size,
        # pool_pre_ping: a WSL Postgres that went away must surface as one failed
        # connection, not as a confusing error on the next statement.
        pool_pre_ping=True,
        future=True,
        connect_args={
            "connect_timeout": connect_timeout,
            # A pipeline statement that hangs forever is worse than one that fails:
            # the nightly budget is finite (spec §9).
            "options": f"-c statement_timeout={statement_timeout_ms}",
        },
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory. `expire_on_commit=False` keeps loaded objects usable after commit."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Session context manager: commits on success, rolls back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database(engine: Engine) -> DatabaseStatus:
    """Probe the database for the health CLI (spec §9).

    Never raises: an unreachable database is a *status*, not an exception — the health
    command must be able to report "down WITH a reason".
    """
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            pgvector = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()
    except Exception as exc:  # any driver/connection failure is a status, not a crash
        return DatabaseStatus(ok=False, detail=f"{type(exc).__name__}: {exc}")

    detail = None
    if pgvector is None:
        detail = "the pgvector extension is not installed in this database"
    return DatabaseStatus(
        ok=True,
        server_version=str(version),
        pgvector_version=str(pgvector) if pgvector is not None else None,
        detail=detail,
    )
