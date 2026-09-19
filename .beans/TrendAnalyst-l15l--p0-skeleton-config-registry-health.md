---
# TrendAnalyst-l15l
title: P0 — Skeleton, config, registry, health
status: completed
type: epic
priority: high
created_at: 2026-09-19T11:42:14Z
updated_at: 2026-09-19T14:12:47Z
---

Phase P0 of the builder brief: skeleton, layered config, the source registry with the
Tier-A-in-L0 hard fail, Postgres models plus the first migration, and a health CLI that
reports an empty-but-healthy system.

DONE WHEN (brief): pytest green, ruff clean, health runs and reports an empty-but-healthy
system. EVIDENCE: docs/evidence/P0-gate.md (raw, generated) and docs/evidence/P0.md (outcome).

## Outcome

All eight tasks completed, in order, one in progress at a time:

- T1 scaffold to the spec §3 shape + pinned toolchain (system Python 3.14.6, no interpreter
  downloaded; ruff/mypy still target py312 so the code stays portable to the spec's Python)
- T2 layered settings (defaults < YAML < env < CLI) with secrets as SecretStr
- T3 the Appendix A registry, 22 sources in execution order, Tier-A-in-L0 a hard failure
- T4 plugin contract + the per-source leash (rate, daily budget, 429 backoff), no network
- T5 the eleven spec §7 tables, append-only triggers, compare-and-swap, migration 0001
- T6 registry -> database sync that never touches a watermark and is a no-op on re-run
- T7 health CLI with exit codes 0/1/2 and structured logs carrying run_id
- T8 the acceptance gate (11 checks, 0 pending) and the evidence log

Final numbers: 261 tests (210 run without a database), ruff clean, mypy --strict clean on
58 files, alembic zero drift, health exit 0. Eight deviations (D1-D8) and five spec
conflicts (C1-C5) are recorded in README.md and docs/evidence/P0.md.

Seven real bugs were found by running things rather than trusting them, the most expensive
being that every Windows->WSL connection took 130 s because `localhost` prefers IPv6 ::1,
which WSL black-holes.
