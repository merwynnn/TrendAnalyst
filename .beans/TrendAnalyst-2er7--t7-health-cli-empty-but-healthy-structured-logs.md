---
# TrendAnalyst-2er7
title: T7 — Health CLI (empty-but-healthy) + structured logs
status: completed
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T14:11:08Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-zai8
    - TrendAnalyst-xpnf
---

- [x] monitor/health.py reads runs, run_source_log, quota_ledger, judge keep-rate, eval baseline
- [x] per-source ok/degraded/down; quota burn vs budget
- [x] python -m trend_analyst.health (spec §9) + ta-health console script; --json for the monitor agent
- [x] exit codes: 0 healthy / 1 degraded / 2 down; DB unreachable = down WITH a reason, never silent success
- [x] structured logs carrying run_id on every line
- [x] tests: empty DB -> healthy; degraded fixture -> exit 1; DB down -> exit 2

DONE WHEN: all three paths behave as above on real runs.
EVIDENCE: pasted into docs/evidence/P0.md

## Summary of Changes

The health CLI, plus the structured logging it depends on. The spec's §9 list is covered:
last run status, per-source ok/degraded/down, quota burn vs budget, judge keep-rate, eval
baseline — and the whole thing never raises.

- **Never raises on an operational problem.** An unreachable database, a missing DSN, an
  invalid registry and a configuration error are each a status (DOWN) with a reason. A
  health check that exits 0 while something is broken is the one failure mode that makes
  monitoring worthless, so the exit code is the contract: 0 healthy / 1 degraded / 2 down.
- **Status rules are pure and tested without a database** (source_status): disabled
  sources are "disabled" (Tier A waiting for a key is not a problem), an untouched source
  is "never_run" (normal on a fresh install), and an otherwise-ok source is escalated to
  degraded when it hit rate limits or burned past the quota alert threshold — because both
  mean tonight collects less than it should.
- **Judge keep-rate is derived from candidates.status** rather than stored in a parallel
  counter, so there is one source of truth for the same fact; the LLM gate telemetry it
  reports is n/a until P3, and says so instead of printing 0 (a fake zero would look like
  a collapse in keep-rate).
- **build_report(session, ...)** is separate from collect_health(engine): the query path
  can be exercised through a session that holds uncommitted rows, which is what makes the
  seeded degraded/down tests possible at all.
- **logging.py**: JSON lines with run_id on every record (a contextvar, so call sites do
  not thread it), structured extras as top-level keys, a human formatter for dev, and
  idempotent configuration (stacked handlers would double every line).

### Verification (real runs)
```
uv run python -m trend_analyst.health                  -> HEALTHY (exit 0)
uv run ta-health                                       -> HEALTHY (exit 0)  [entry point]
bash scripts/db_down.sh && uv run python -m ...health   -> DOWN (exit 2), reason:
     "database unreachable: OperationalError: (psycopg.errors.ConnectionTimeout) ..."
seeded failed source                                   -> DOWN (exit 2)
seeded degraded source                                 -> DEGRADED (exit 1)
seeded 429s on an ok source                            -> DEGRADED (exit 1)
seeded 85% burn on a 100/day source                    -> DEGRADED (exit 1), listed in over_threshold
uv run pytest -q                                       -> 261 passed
uv run ruff check .                                    -> All checks passed!
uv run mypy src config scripts                          -> no issues (58 files)
bash scripts/gate.sh                                    -> GATE: PASS (0 pending)
```
