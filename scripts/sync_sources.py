"""Sync `config/sources.yaml` into the `sources` table (spec §7: "Registry mirror").

    uv run python -m scripts.sync_sources
    uv run python -m scripts.sync_sources --dry-run
    uv run python -m scripts.sync_sources --json

The registry is the source of truth for every registry-owned column: tier, layers,
schedule, budgets, rps, cache TTL, enabled and the egress allowlist. This script copies
it into the database so the pipeline, the health CLI and SQL queries can all see the same
configuration without parsing YAML.

Two rules make it safe to run at any time:

* **The watermark is never written.** It is runtime state owned by the pipeline (spec
  §5.1), not configuration. A sync must not be able to make a source re-fetch or skip
  work — so the update statement simply does not mention those columns.
* **Nothing is deleted.** A source that disappears from the registry is disabled in
  place, because `signals`, `raw_items` and `quota_ledger` rows still reference its id,
  and history outlives configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.sources.registry import (
    Registry,
    RegistryError,
    default_registry_path,
    load_registry,
)
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from trend_analyst.store.models import Source

__all__ = ["SyncReport", "main", "sync_sources"]

#: Columns the registry owns. `watermark` and `watermark_updated_at` are deliberately
#: absent: they belong to the pipeline, and a configuration sync must never touch them.
REGISTRY_OWNED_COLUMNS: tuple[str, ...] = (
    "role",
    "tier",
    "layers",
    "schedule",
    "budget_per_day",
    "rps",
    "cache_ttl_h",
    "enabled",
    "domains",
)


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What the sync did, so a human (or the monitor agent) can read it and move on."""

    inserted: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    disabled_missing: tuple[str, ...] = ()
    already_disabled: tuple[str, ...] = ()
    dry_run: bool = False
    changes: dict[str, dict[str, tuple[Any, Any]]] = field(default_factory=dict)

    @property
    def changed(self) -> tuple[str, ...]:
        return self.inserted + self.updated + self.disabled_missing

    def summary(self) -> dict[str, Any]:
        return {
            "inserted": len(self.inserted),
            "updated": len(self.updated),
            "unchanged": len(self.unchanged),
            "disabled_missing": len(self.disabled_missing),
            "already_disabled": len(self.already_disabled),
            "dry_run": self.dry_run,
            "ids": {
                "inserted": list(self.inserted),
                "updated": list(self.updated),
                "disabled_missing": list(self.disabled_missing),
                "already_disabled": list(self.already_disabled),
            },
            "changes": {
                key: {name: list(pair) for name, pair in value.items()}
                for key, value in self.changes.items()
            },
        }

    def render(self) -> str:
        mode = "DRY RUN — nothing written" if self.dry_run else "applied"
        lines = [
            f"registry sync ({mode})",
            f"  inserted:         {len(self.inserted)}",
            f"  updated:          {len(self.updated)}",
            f"  unchanged:        {len(self.unchanged)}",
            f"  disabled (gone from the registry): {len(self.disabled_missing)}",
            f"  already disabled (gone earlier):   {len(self.already_disabled)}",
        ]
        for source_id, fields in self.changes.items():
            rendered = ", ".join(
                f"{name}: {old!r} -> {new!r}" for name, (old, new) in fields.items()
            )
            lines.append(f"    {source_id}: {rendered}")
        return "\n".join(lines)


def _values_for(entry: Any) -> dict[str, Any]:
    """The registry-owned values of one registry entry, ready for the `sources` row."""
    return {
        "role": entry.role,
        "tier": entry.tier,
        "layers": list(entry.layers),
        "schedule": entry.schedule,
        "budget_per_day": entry.budget_per_day,
        "rps": entry.rps,
        "cache_ttl_h": entry.cache_ttl_h,
        "enabled": entry.enabled,
        "domains": list(entry.domains),
    }


def _differences(row: Source, values: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Which registry-owned columns differ from the stored row.

    `layers` and `domains` are compared as lists because Postgres hands them back as
    lists; everything else compares directly.
    """
    differences: dict[str, tuple[Any, Any]] = {}
    for column in REGISTRY_OWNED_COLUMNS:
        stored = getattr(row, column)
        wanted = values[column]
        if isinstance(wanted, list) and isinstance(stored, list):
            if list(stored) != wanted:
                differences[column] = (list(stored), wanted)
        elif stored != wanted:
            differences[column] = (stored, wanted)
    return differences


def sync_sources(session: Session, registry: Registry, *, dry_run: bool = False) -> SyncReport:
    """Upsert the registry into the `sources` table. Never touches watermarks.

    Args:
        session: an open database session.
        registry: a validated registry.
        dry_run: when True, report what would change and write nothing.
    """
    existing = {row.id: row for row in session.execute(select(Source)).scalars()}

    inserted: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    changes: dict[str, dict[str, tuple[Any, Any]]] = {}

    for entry in registry.sources:
        values = _values_for(entry)
        row = existing.pop(entry.id, None)
        if row is None:
            inserted.append(entry.id)
            if not dry_run:
                session.add(Source(id=entry.id, **values))
            continue
        differences = _differences(row, values)
        if not differences:
            unchanged.append(entry.id)
            continue
        updated.append(entry.id)
        changes[entry.id] = differences
        if not dry_run:
            for column, (_old, new) in differences.items():
                setattr(row, column, new)

    # Rows left in `existing` are gone from the registry: disable, never delete (history
    # outlives configuration, and these ids are referenced by other tables).
    disabled_missing: list[str] = []
    already_disabled: list[str] = []
    for source_id in sorted(existing):
        row = existing[source_id]
        if row.enabled:
            disabled_missing.append(source_id)
            changes.setdefault(source_id, {})["enabled"] = (True, False)
            if not dry_run:
                row.enabled = False
        else:
            already_disabled.append(source_id)

    if not dry_run:
        session.flush()

    return SyncReport(
        inserted=tuple(inserted),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        disabled_missing=tuple(disabled_missing),
        already_disabled=tuple(already_disabled),
        dry_run=dry_run,
        changes=changes,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.sync_sources",
        description="Copy config/sources.yaml into the sources table (watermarks untouched).",
    )
    parser.add_argument("--registry", default=None, help="path to sources.yaml")
    parser.add_argument("--config-dir", default=None, help="directory holding sources.yaml")
    parser.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    registry_path = Path(args.registry) if args.registry else default_registry_path(config_dir)

    try:
        registry = load_registry(registry_path)
    except RegistryError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        return 1

    try:
        settings = load_settings(config_dir)
        engine: Engine = create_db_engine(settings)
    except (ConfigError, DatabaseNotConfiguredError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        factory = create_session_factory(engine)
        with session_scope(factory) as session:
            report = sync_sources(session, registry, dry_run=args.dry_run)
    finally:
        engine.dispose()

    if args.json:
        print(json.dumps({"registry": str(registry_path), **report.summary()}, indent=2))
    else:
        print(f"registry: {registry_path} ({len(registry.sources)} entries)")
        print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
