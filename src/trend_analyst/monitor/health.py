"""Health CLI (spec §9): last run status, per-source ok/degraded/down, quota burn vs
budget, judge keep-rate, eval baseline. Exit 0 healthy / 1 degraded / 2 down.

STUB — implemented in phase P0-T7. This module exports nothing and does nothing on
purpose: a later phase fills it in. It exists so the repository matches the layout the
spec mandates (section 3) and so imports stay stable.
"""

from __future__ import annotations

STUB_PHASE = "P0-T7"


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m trend_analyst.health` and the `ta-health` script."""
    raise NotImplementedError("P0-T7: the health CLI is implemented in phase P0-T7")
