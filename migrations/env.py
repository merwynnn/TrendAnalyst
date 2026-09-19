"""Alembic environment.

The database URL is resolved at runtime, in this order:

1. ``-x db_url=...`` on the command line (used by CI, which has no secrets file),
2. ``sqlalchemy.url`` in alembic.ini, if someone filled it in,
3. the layered settings — i.e. ``config/secrets.local.yaml``.

Deliberately not in alembic.ini: that file is committed, and a committed DSN is a leaked
DSN. A missing URL raises with the command that provisions one.
"""

from __future__ import annotations

import os
import sys

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from config.settings import ConfigError, load_settings
from trend_analyst.store.models import Base

config = context.config
target_metadata = Base.metadata


def _trace(message: str) -> None:
    """Opt-in progress trace (``TA_MIGRATION_TRACE=1``).

    Migration startup touches config, settings, the driver and the server. When one of
    those blocks, the process emits nothing at all, which is impossible to diagnose from
    the outside — this makes the last completed step visible.
    """
    if os.environ.get("TA_MIGRATION_TRACE"):
        # stderr directly, not print: this runs before alembic's logging is configured,
        # and the trace must appear even when the process is captured mid-hang.
        sys.stderr.write(f"[alembic/env.py] {message}\n")
        sys.stderr.flush()


def _database_url() -> str:
    supplied = context.get_x_argument(as_dictionary=True).get("db_url")
    if supplied:
        return supplied

    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured

    settings = load_settings()
    if not settings.db.configured:
        raise ConfigError(
            "no database URL available for migrations. Run "
            "`bash scripts/provision_pg.sh` (it writes db.url into "
            "config/secrets.local.yaml) or pass -x db_url=postgresql+psycopg://..."
        )
    return settings.db.dsn


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


#: A migration that waits forever is worse than one that fails: DDL takes an ACCESS
#: EXCLUSIVE lock, so a stray open transaction elsewhere would otherwise hang the deploy.
_MIGRATION_CONNECT_ARGS: dict[str, object] = {
    "connect_timeout": 10,
    "options": "-c lock_timeout=10s -c statement_timeout=120000",
}


def run_migrations_online() -> None:
    """Connect and run the migrations."""
    _trace("resolving the database URL")
    resolved = _database_url()
    _trace("database URL resolved")
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = resolved
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_MIGRATION_CONNECT_ARGS,
    )
    _trace("engine built; connecting")
    with connectable.connect() as connection:
        _trace("connected; running migrations")
        _run(connection)
    _trace("migrations finished")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
