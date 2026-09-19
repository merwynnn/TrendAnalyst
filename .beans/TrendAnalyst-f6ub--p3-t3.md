---
# TrendAnalyst-f6ub
title: P3-T3
status: completed
type: task
created_at: 2026-09-19T17:26:31Z
updated_at: 2026-09-19T17:26:31Z
---

- [x] llm/gates.py: batch candidates (10/call), evidence in one query, prompt with instructions
- [x] verdicts applied to candidates.status (kept/dropped) only for overwritable statuses
- [x] migration 0002 + store models: append-only judgements with citations, enrich, model, cache key, tokens
- [x] a degraded gate changes NOTHING (an outage must not read as a mass deletion)
- [x] a verdict naming an unknown phrase is counted and ignored; missing verdicts counted
- [x] CLI: --judge (live) and --judge-replay (offline), with --fresh to bypass the cache
- [x] tests: 20 in tests/test_gates.py

DONE WHEN: the gate is exercised offline and its verdicts are durable, cited and append-only.
EVIDENCE: gate check "P3 judge - offline replay of a recorded verdict" PASS
