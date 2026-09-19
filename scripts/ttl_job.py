"""The TTL job: run it nightly, or weekly — nothing here is time-critical (spec §5.4, brief P4).

    uv run python -m scripts.ttl_job            # delete what expired
    uv run python -m scripts.ttl_job --dry-run  # report what would go

Raw lake rows older than 90 days go; the LLM cache sweeps its own 30-day TTL in the same pass;
scores, judgements, briefs and the quota ledger are counted and left alone. Those counts are printed
every run on purpose: a retention job that reports only deletions gives no evidence that it is not
deleting history.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.ttl import DEFAULT_RAW_TTL_DAYS, expire

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ttl_job",
        description="Expire the raw lake and the LLM cache; never the ledger or the snapshots.",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_RAW_TTL_DAYS, help="raw lake retention")
    parser.add_argument("--dry-run", action="store_true", help="report, delete nothing")
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        load_config = load_settings(config_dir)
        del load_config
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        engine = create_db_engine()
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    sessions = create_session_factory(engine)
    try:
        with sessions() as session:
            report = expire(session, raw_ttl_days=args.days, dry_run=args.dry_run)
            if not args.dry_run:
                session.commit()
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.summary())
        for table, count in sorted(report.protected.items()):
            print(f"  protected: {table:<12} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
