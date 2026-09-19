---
# TrendAnalyst-l90u
title: P1-T2 — Record fixtures for the three collectors (live, once)
status: completed
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:39:03Z
parent: TrendAnalyst-8dtf
blocked_by:
    - TrendAnalyst-ph72
---

- [x] scripts/record_fixtures.py runs the real client against the real endpoints and writes tests/data/http/<source>.json
- [x] fixtures are committed, tiny (bounded item counts) and contain no credentials
- [x] a fixture is recorded for hn_firebase, wiki_pageviews and arctic_shift
- [x] recorder is manual-only: tests never call it, and a test asserts every fixture file is valid JSON with a recorded_at stamp

DONE WHEN: three fixtures exist and replay offline.
EVIDENCE: pasted into docs/evidence/P1.md

## Summary of Changes

Recorded from the live APIs, once, deliberately:

```
hn_firebase     13 responses   27 KB   pass 1: 13 req, 12 stories, 12 signals
                                       pass 2:  1 req, not_modified, 0 signals
wiki_pageviews   3 responses  188 KB   pass 1:  3 req, 3 days, 2990 signals
                                       pass 2:  0 req, not_modified
arctic_shift    30 responses  620 KB   pass 1: 15 req, 71 signals
                                       pass 2: 15 req (after=<watermark>), 67 signals
```

- **Two passes per source by default.** The first is a cold start; the second runs with the
  cursor the first produced. Recording only the cold start would leave the watermark path
  untested, and the watermark path is most of P1 — the replay above is *evidence* that a
  repeat run costs 1 request for HN and 0 for Wikipedia.
- Bounded: per-source item limits are small (`hn 12, wiki 3 days, arctic 5/sub`), and
  the limits are stamped into the fixture so a replay reissues exactly the recorded URLs.
- Polite: the recorder drives the real plugin through the same SourceBudget the pipeline
  uses, so it makes exactly the requests a run would, at the registry's rps, capped at a
  small recording budget.
- Hygiene is enforced by tests: every fixture is JSON, carries `recorded_at`, and mentions
  no cookie, authorization header, api key or bearer token.
