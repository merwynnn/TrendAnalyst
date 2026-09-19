---
# TrendAnalyst-gp67
title: P1-T3 — Three Tier-S collectors against the contract
status: completed
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:39:03Z
parent: TrendAnalyst-8dtf
blocked_by:
    - TrendAnalyst-l90u
---

- [x] tier_s/hn.py, tier_s/wiki.py, tier_s/arctic_shift.py implement SourcePlugin (fetch + parse)
- [x] every request goes through ctx.budget.try_acquire() first; a refusal returns cleanly (the run continues without that source)
- [x] responses feed ctx.budget.record_response(status, retry_after_s) so 429s start backoff
- [x] cursors are returned on RawBatch and stored as the source watermark (spec §5.1); an unchanged source reports not_modified instead of re-parsing
- [x] parse() is pure: no I/O, and the recorded fixtures cover it
- [x] tests: each plugin parses its fixture into signals with the expected entities/metrics; budgets are respected; a 429 fixture degrades the source instead of raising

DONE WHEN: tests green, mypy --strict clean, zero network.
EVIDENCE: pasted into docs/evidence/P1.md

## Summary of Changes

Three Tier-S collectors, each validated against its registry entry by load_plugin():

- **hn_firebase** — front page then one request per story. The cursor is the front-page id
  list, so an unchanged front page returns not_modified after ONE request instead of 61.
- **wiki_pageviews** — the top-1000 articles per complete UTC day. The cursor is the newest
  day collected, and the cold start reaches back three days; a long gap is caught up three
  days at a time, oldest first (never by jumping to the newest days, which would silently
  skip the middle). A 404 for an unpublished day is normal and skipped.
- **arctic_shift** — 15 pain-mining subreddits, one request each, with the watermark passed
  back as the API's after= so an incremental run asks only for what is new.

Design points that matter downstream:
- **SourceSkippedError** marks "we did not run this, for this reason" (budget exhausted, backing
  off) — distinct from an empty batch, so the ledger and health never report a skip as a zero.
- **RawBatch.not_modified** marks "the source reports nothing new", which lets the
  orchestrator skip parsing, scoring and the LLM entirely.
- parse() is pure and defensive: malformed parts are skipped, never guessed at, and both
  documented API response shapes are accepted.
- The plugins' declared domains must equal the registry's, which validate_plugin() enforces:
  a collector cannot widen its own egress.

42 tests over the three fixtures, including a 429 that degrades instead of raising, a
budget that runs out mid-sweep (partial batch, no crash), and the fixture-hygiene checks.
