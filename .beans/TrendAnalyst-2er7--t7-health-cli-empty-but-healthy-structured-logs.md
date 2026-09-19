---
# TrendAnalyst-2er7
title: T7 — Health CLI (empty-but-healthy) + structured logs
status: in-progress
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T14:02:20Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-zai8
    - TrendAnalyst-xpnf
---

- [ ] monitor/health.py reads runs, run_source_log, quota_ledger, judge keep-rate, eval baseline
- [ ] per-source ok/degraded/down; quota burn vs budget
- [ ] python -m trend_analyst.health (spec §9) + ta-health console script; --json for the monitor agent
- [ ] exit codes: 0 healthy / 1 degraded / 2 down; DB unreachable = down WITH a reason, never silent success
- [ ] structured logs carrying run_id on every line
- [ ] tests: empty DB -> healthy; degraded fixture -> exit 1; DB down -> exit 2

DONE WHEN: all three paths behave as above on real runs.
EVIDENCE: pasted into docs/evidence/P0.md
