---
# TrendAnalyst-8dtf
title: P1 — L0 collectors + run ledger
status: completed
type: epic
priority: high
created_at: 2026-09-19T14:15:07Z
updated_at: 2026-09-19T14:54:47Z
---

Phase P1 of the builder brief: three Tier-S collectors plus the ledger, watermarks,
content-hash dedup and crash recovery.

DONE WHEN (brief): two consecutive nightly-equivalent runs, the second near-zero new work,
proven by the ledger; plus kill-and-resume executing only unfinished work.
EVIDENCE: docs/evidence/P1-gate.md (raw, generated) and docs/evidence/P1.md (outcome).

## Outcome

All six tasks completed, one in progress at a time:

- P1-T1 the HTTP client: egress allowlist before the request, no redirect following,
  timeouts, bounded retries, 429 handed back to the budget, injected sleeps
- P1-T2 fixtures recorded once from the live APIs (two passes each: cold start and the
  incremental run the watermark produces), replayed forever
- P1-T3 three collectors — Hacker News, Wikipedia pageviews, Arctic Shift — each validated
  against its registry entry, each with a pure parse()
- P1-T4 the run ledger, watermark discipline, quota compare-and-swap and budget rehydration
- P1-T5 the L0 orchestrator with per-source isolation, two dedup layers and resume, plus an
  offline replay mode that makes the whole layer testable without network
- P1-T6 the gate (12 checks, 0 pending) and the outcome document

Final numbers: 337 tests (no test touches the network), ruff clean, mypy --strict clean on
61 files, gate PASS with 0 pending. Acceptance evidence: 3073 signals on a cold run, then 0
new and 0 parsed on the repeat; the crash drill resumes into exactly the unfinished sources.

Seven bugs were found by running things rather than trusting them, the most consequential
being that the leash refused instead of pacing — which had silently collected 3 of 15
subreddits.
