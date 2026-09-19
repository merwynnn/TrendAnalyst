"""``python -m config.cli`` — inspect the resolved configuration, secrets redacted.

    uv run python -m config.cli show
    uv run python -m config.cli show --json
    uv run python -m config.cli show --env prod
    uv run python -m config.cli show --set app.log_level=DEBUG --set budgets.top_k=5

The point of this command is to answer "what is the system ACTUALLY configured with?"
without ever printing a credential: secrets render as ``***`` when configured and as an
empty string when not, and :meth:`Settings.secret_status` lists which keys exist without
revealing any of them. Operators and the monitor agent use it; nothing in the pipeline
needs it.

Exit codes: 0 success, 1 bad configuration (actionable message on stderr).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from config.settings import (
    ConfigError,
    Settings,
    default_config_dir,
    describe_sources,
    load_settings,
    parse_overrides,
)
from trend_analyst.sources.registry import (
    Registry,
    RegistryError,
    default_registry_path,
    load_registry,
)

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m config.cli",
        description="Inspect Trend Analyst's resolved configuration (secrets redacted).",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    show = subcommands.add_parser("show", help="print the resolved settings")
    show.add_argument("--config-dir", default=None, help="directory holding settings*.yaml")
    show.add_argument("--env", default=None, help="environment name (default: TA_ENV or dev)")
    show.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help="highest-precedence override; repeatable",
    )
    show.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    registry = subcommands.add_parser(
        "registry",
        help="validate the source registry and print it in execution order",
    )
    registry.add_argument(
        "--registry", default=None, help="path to sources.yaml (default: <config-dir>/sources.yaml)"
    )
    registry.add_argument("--config-dir", default=None, help="directory holding sources.yaml")
    registry.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def _layer_report(settings: Settings) -> list[dict[str, object]]:
    return [
        {"label": layer.label, "path": str(layer.path), "exists": layer.exists}
        for layer in describe_sources(settings.app.config_dir, settings.app.env)
    ]


def _render_text(settings: Settings) -> str:
    configured = sorted(name for name, ok in settings.secret_status().items() if ok)
    credentials = (
        f"credentials configured: {len(configured)} -> {', '.join(configured)}"
        if configured
        else "credentials configured: none (Tier-A sources stay disabled)"
    )
    lines = [
        "Trend Analyst configuration (secrets redacted)",
        "",
        "layers, highest precedence first:",
        *(
            f"  [{'loaded' if layer['exists'] else 'absent':>6}] {layer['label']}  {layer['path']}"
            for layer in _layer_report(settings)
        ),
        "",
        json.dumps(settings.redacted(), indent=2, default=str),
        "",
        credentials,
    ]
    return "\n".join(lines)


def _render_registry_text(registry: Registry) -> str:
    columns = ("id", "tier", "layers", "schedule", "on", "budget/day", "rps", "domains")
    rows = [
        (
            entry.id,
            entry.tier,
            ",".join(entry.layers),
            entry.schedule,
            "yes" if entry.enabled else "no",
            str(entry.budget_per_day),
            f"{entry.rps:g}",
            str(len(entry.domains)),
        )
        for entry in registry.sources
    ]
    widths = [max(len(columns[i]), *(len(row[i]) for row in rows)) for i in range(len(columns))]
    lines = [
        "Trend Analyst source registry — file order IS execution order",
        "",
        "  ".join(columns[i].ljust(widths[i]) for i in range(len(columns))),
        "  ".join("-" * widths[i] for i in range(len(columns))),
        *(
            "  ".join(row[i].ljust(widths[i]) for i in range(len(columns)))
            for row in rows
        ),
        "",
    ]
    summary = registry.summary()
    lines.append(
        f"{summary['total']} sources ({summary['tier_s']} Tier S, {summary['tier_a']} Tier A) · "
        f"{summary['enabled']} enabled, {summary['disabled']} disabled"
    )
    lines.append(
        f"L0 (collect): {summary['l0']} · L2 (enrich): {summary['l2']} · "
        f"daily budget of enabled sources: {summary['daily_budget']} requests"
    )
    return "\n".join(lines)


def _run_registry_command(args: argparse.Namespace) -> int:
    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    registry_path = Path(args.registry) if args.registry else default_registry_path(config_dir)
    try:
        registry = load_registry(registry_path)
    except RegistryError as exc:
        print(f"registry error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "version": registry.version,
            "summary": registry.summary(),
            "sources": [entry.model_dump(mode="json") for entry in registry.sources],
            "allowed_domains": registry.allowed_domains(),
        }
        print(json.dumps(payload, indent=2))
    else:
        print(_render_registry_text(registry))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "registry":
        return _run_registry_command(args)

    try:
        settings = load_settings(
            args.config_dir,
            env_name=args.env,
            cli_overrides=parse_overrides(args.overrides),
        )
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    if args.command != "show":  # argparse enforces choices; this keeps mypy honest
        parser.error(f"unknown command: {args.command}")

    payload: dict[str, Any] = {
        "settings": settings.redacted(),
        "credentials_configured": dict(sorted(settings.secret_status().items())),
        "layers": _layer_report(settings),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(_render_text(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
