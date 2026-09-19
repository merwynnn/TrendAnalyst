---
# TrendAnalyst-ioh9
title: P1-T4 — Run ledger, watermarks, checkpoints and quota CAS
status: in-progress
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:39:03Z
parent: TrendAnalyst-8dtf
blocked_by:
    - TrendAnalyst-l90u
---

- [ ] pipeline/state.py: start_run / finish_run / record_source_result writing runs and run_source_log
- [ ] watermark read + conditional advance (never moves backwards, never touches another source's row)
- [ ] record_spend() writes quota_ledger with the compare-and-swap key, treating a duplicate as already-recorded instead of double-spending
- [ ] completed_sources(run_id) + pending_sources(run_id) implement "resume executes only unfinished work"
- [ ] tests: ledger rows, watermark discipline, duplicate spend is a no-op, resume selection

DONE WHEN: tests green against Postgres, and the CAS path is proven under a real unique-violation.
EVIDENCE: pasted into docs/evidence/P1.md
