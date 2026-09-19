"""`python -m trend_analyst.health` — the health CLI entry point of spec §9.

Spec conflict C1: section 3 of the spec puts the health module in `monitor/health.py`,
while section 9 mandates this module path (`python -m trend_analyst.health`). The
implementation lives in `monitor/health.py` and this module re-exports it, so both
statements hold. Keep it thin — logic belongs in the monitor package.
"""

from __future__ import annotations

from trend_analyst.monitor.health import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
