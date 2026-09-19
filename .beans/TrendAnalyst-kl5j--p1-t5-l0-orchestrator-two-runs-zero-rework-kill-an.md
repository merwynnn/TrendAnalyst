---
# TrendAnalyst-kl5j
title: 'P1-T5 — L0 orchestrator: two runs, zero rework, kill-and-resume'
status: todo
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:15:52Z
parent: TrendAnalyst-8dtf
blocked_by:
    - TrendAnalyst-gp67
    - TrendAnalyst-ioh9
---

- [ ] pipeline/orchestrator.py runs L0 for every enabled L0 source in registry order
- [ ] per source: budget, fetch, content-hash dedup (spec §5.2), raw lake write, signals write, watermark, ledger
- [ ] resume=True skips sources already completed in that run and retries only failed/unfinished ones
- [ ] ACCEPTANCE 1: two consecutive runs over the same fixtures -> the second parses and stores zero new signals, proven by ledger counts
- [ ] ACCEPTANCE 2: a run interrupted after N sources resumes and executes ONLY the remaining sources (asserted on the client's request log)
- [ ] tests: both acceptances plus a source-level failure that does not abort the run

DONE WHEN: both acceptances pass against Postgres with recorded fixtures.
EVIDENCE: pasted into docs/evidence/P1.md
