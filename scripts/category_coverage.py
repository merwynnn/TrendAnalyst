"""Report how much of the real lake the taxonomy actually covers.

    uv run python -m scripts.category_coverage          # from the database
    uv run python -m scripts.category_coverage --json

Why this exists: a taxonomy is a claim about the data, and a claim that is never measured
drifts into fiction. This prints the match rate per source and the category spread, so a
taxonomy that matches almost nothing is visible as a number rather than discovered later
as a pipeline that produces no candidates.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select

from config.categories import default_taxonomy
from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
)
from trend_analyst.store.models import SignalRow

__all__ = ["main"]


def _phrases_by_source(limit_per_source: int | None = None) -> dict[str, list[str]]:
    engine = create_db_engine()
    factory = create_session_factory(engine)
    grouped: dict[str, list[str]] = {}
    try:
        with factory() as session:
            rows = session.execute(select(SignalRow.source_id, SignalRow.entity)).all()
        for source_id, entity in rows:
            grouped.setdefault(str(source_id), []).append(str(entity))
    finally:
        engine.dispose()
    if limit_per_source is not None:
        return {source: phrases[:limit_per_source] for source, phrases in grouped.items()}
    return grouped


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.category_coverage",
        description="How much of the collected text the category taxonomy matches.",
    )
    parser.add_argument("--config-dir", default=None, help="directory holding categories.yaml")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    try:
        load_settings(config_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    taxonomy = default_taxonomy(config_dir)
    try:
        by_source = _phrases_by_source()
    except DatabaseNotConfiguredError as exc:
        print(f"database not configured: {exc}", file=sys.stderr)
        return 1

    sources: dict[str, Any] = {}
    report: dict[str, Any] = {"taxonomy_version": taxonomy.version, "sources": sources}
    all_phrases: list[str] = []
    for source_id, phrases in sorted(by_source.items()):
        all_phrases.extend(phrases)
        sources[source_id] = taxonomy.coverage(phrases)
    report["overall"] = taxonomy.coverage(all_phrases)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    overall: dict[str, Any] = report["overall"]
    print(f"taxonomy v{taxonomy.version} - {len(taxonomy.categories)} categories")
    print(f"overall: {overall['matched']}/{overall['phrases']} phrases matched "
          f"({overall['matched_pct']}%)")
    print("by category:", json.dumps(overall["by_category"]))
    for source_id, stats in sources.items():
        print(
            f"  {source_id:<20} {stats['matched']:>5}/{stats['phrases']:<5}"
            f" ({stats['matched_pct']}%)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
