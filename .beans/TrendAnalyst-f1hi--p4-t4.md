---
# TrendAnalyst-f1hi
title: P4-T4
status: completed
type: task
created_at: 2026-09-19T18:07:20Z
updated_at: 2026-09-19T18:07:20Z
---

- [x] brief rendering (Writer gate + deterministic markdown)
- [x] store/history.py + --history/--history-delta/--compare-versions
- [x] store/ttl.py + scripts/ttl_job.py (raw 90d, cache 30d, ledger and snapshots never)
- [x] evals/run_evals.py: the golden cases executed, with the LLM rubric explicitly not claimed
- [x] .github/workflows/ci.yml: tests, lint, types, migrations, determinism, judge replay, nightly, evals
- [x] README roadmap + deviations D14/D15; AGENT.md commands

DONE WHEN: every claim above has raw output behind it.
EVIDENCE: bash scripts/gate.sh -> GATE: PASS (0 pending)
