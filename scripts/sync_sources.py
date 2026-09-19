"""Sync `config/sources.yaml` into the `sources` table (spec §7: "Registry mirror").

    uv run python -m scripts.sync_sources
    uv run python -m scripts.sync_sources --dry-run
    uv run python -m scripts.sync_sources --json

The logic lives in `trend_analyst.store.sync`, because the orchestrator needs it too: a
source's watermark lives on its row, so a run syncs the registry before it starts. This
module is only the command-line front door — enough to review a registry edit by hand
before it lands.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import Engine

from config.settings import ConfigError, default_config_dir, load_settings
from trend_analyst.sources.registry import RegistryError, default_registry_path, load_registry
from trend_analyst.store.db import (
    DatabaseNotConfiguredError,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from trend_analyst.store.sync import sync_sources

__all__ = ["main"]


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
