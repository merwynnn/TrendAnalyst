---
# TrendAnalyst-96uw
title: T4 — Plugin contract + token bucket (no network)
status: in-progress
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:35:23Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-zai8
---

- [ ] sources/base.py — SourcePlugin ABC: id, tier, layers, budget_per_day, rps, cache_ttl_h, schedule, fetch(ctx)->RawBatch, parse(raw)->list[Signal]
- [ ] boundary models: RawBatch, Signal (Pydantic v2, typed)
- [ ] token bucket per source id + 429 backoff hook + quota_spend accounting (pure; clock injected, no sleep in tests)
- [ ] tests: contract invariants, bucket math, 429 records spend and backs off

DONE WHEN: tests green, mypy --strict clean, zero network calls.
EVIDENCE: pasted into docs/evidence/P0.md
