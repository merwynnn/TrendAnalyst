---
# TrendAnalyst-ioh9
title: P1-T4 — Run ledger, watermarks, checkpoints and quota CAS
status: completed
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:54:47Z
parent: TrendAnalyst-8dtf
blocked_by:
    - TrendAnalyst-l90u
---

- [x] pipeline/state.py: start_run / finish_run / record_source_result writing runs and run_source_log
- [x] watermark read + conditional advance (never moves backwards, never touches another source's row)
- [x] record_spend() writes quota_ledger with the compare-and-swap key, treating a duplicate as already-recorded instead of double-spending
- [x] completed_sources(run_id) + pending_sources(run_id) implement "resume executes only unfinished work"
- [x] tests: ledger rows, watermark discipline, duplicate spend is a no-op, resume selection

DONE WHEN: tests green against Postgres, and the CAS path is proven under a real unique-violation.
EVIDENCE: pasted into docs/evidence/P1.md

## Summary of Changes

pipeline/state.py — the three rules of spec §5 that turn a script into a resumable system:

- **Every run writes a ledger row per (run, source)**, upserted so a retry replaces the
  verdict instead of adding a second one. A failed source is NOT finished (the resume
  retries it); a skipped one IS (it was a decision, and retrying would defeat the backoff
  it was skipped for).
- **A resume is a query, not a flag in memory**: completed_sources / pending_sources read
  the ledger, so "what is left" survives the process that died.
- **Quota is compare-and-swap** (spec §5.4): the unique key on (run, source, operation) is
  the CAS, so a resumed run re-issuing a spend loses the race and the amount is never
  charged twice. record_spend returns False for "already recorded" instead of raising,
  because that is a normal outcome on a resume, not an error.
- **Watermarks refuse two things**: clearing (a source that cannot say where it got to must
  not erase where it was) and rewriting an identical cursor (a no-op pass leaves the
  timestamp alone).
- **restore_budgets() rehydrates today's spend from the ledger**, so a crash is not a way to
  spend a daily budget twice. It shares the caller's clock, so a live run paces against real
  time and a replay paces against the pinned one.

### Verification
```
uv run pytest tests/test_orchestrator.py -q  ->  16 passed (ledger, CAS, watermarks, resume,
                                                 rehydration, dry run, both acceptances)
```
