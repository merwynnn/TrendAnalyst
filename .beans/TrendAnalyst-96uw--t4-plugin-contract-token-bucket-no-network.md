---
# TrendAnalyst-96uw
title: T4 — Plugin contract + token bucket (no network)
status: completed
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:38:01Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-zai8
---

- [x] sources/base.py — SourcePlugin ABC: id, tier, layers, budget_per_day, rps, cache_ttl_h, schedule, fetch(ctx)->RawBatch, parse(raw)->list[Signal]
- [x] boundary models: RawBatch, Signal (Pydantic v2, typed)
- [x] token bucket per source id + 429 backoff hook + quota_spend accounting (pure; clock injected, no sleep in tests)
- [x] tests: contract invariants, bucket math, 429 records spend and backs off

DONE WHEN: tests green, mypy --strict clean, zero network calls.
EVIDENCE: pasted into docs/evidence/P0.md

## Summary of Changes

The plugin contract with a leash that cannot be escaped, and a seam that keeps tests offline.

- **sources/base.py** — SourcePlugin ABC (fetch/parse are @abstractmethod, so an
  incomplete plugin cannot even be instantiated), RawBatch and Signal boundary models
  (frozen, extra=forbid, timezone-aware timestamps enforced), FetchContext, and the
  budget machinery. Zero HTTP imports: the client is a Protocol, and a test parses the
  module with ast to prove nothing network-capable is imported.
- **Hashing (spec §5.2)** — normalize_payload() + content_hash(), with the hash DERIVED
  from the payloads rather than stored (a stored hash can go stale and lie). The hash is
  injective across part boundaries, and whitespace/line-ending-only changes do not look
  like new content — that is the mechanism that keeps nightly runs near 10 minutes.
- **The leash (spec §4.3)** — TokenBucket (rate) + SourceBudget (rate + daily budget +
  429 backoff), all pure, with time injected. try_acquire() returns one of
  ok | rate_limited | budget_exhausted | backing_off, so the ledger never records a
  mysterious stall. An attempted request — including the one that earns the 429 — is
  charged to the budget, and Retry-After is honoured and capped.
- **Registry gate** — validate_plugin() fails when a plugin disagrees with its registry
  entry on tier, layers, schedule, budgets or **the egress allowlist** (a plugin cannot
  widen its own network permissions), and load_plugin() implements the whole
  add-a-source lifecycle: import, verify, instantiate.

### Verification
```
uv run pytest -q        ->  176 passed   (51 new tests, zero sleeps: a fake clock advances)
uv run ruff check .     ->  All checks passed!
uv run mypy src config  ->  Success: no issues found in 56 source files
```

### Design flaw caught by the tests
The first version of SourcePlugin declared fetch/parse as plain methods, so
`inspect.isabstract` was False and an incomplete plugin could be instantiated (its
methods would raise only when called). They are now @abstractmethod.
