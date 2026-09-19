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
from typing import Any

from config.settings import (
    ConfigError,
    Settings,
    describe_sources,
    load_settings,
    parse_overrides,
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

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
