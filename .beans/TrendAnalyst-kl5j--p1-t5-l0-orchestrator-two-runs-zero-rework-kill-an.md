---
# TrendAnalyst-kl5j
title: 'P1-T5 — L0 orchestrator: two runs, zero rework, kill-and-resume'
status: completed
type: task
priority: high
created_at: 2026-09-19T14:15:51Z
updated_at: 2026-09-19T14:54:47Z
parent: TrendAnalyst-8dtf
blocked_by:
    - TrendAnalyst-gp67
    - TrendAnalyst-ioh9
---

- [x] pipeline/orchestrator.py runs L0 for every enabled L0 source in registry order
- [x] per source: budget, fetch, content-hash dedup (spec §5.2), raw lake write, signals write, watermark, ledger
- [x] resume=True skips sources already completed in that run and retries only failed/unfinished ones
- [x] ACCEPTANCE 1: two consecutive runs over the same fixtures -> the second parses and stores zero new signals, proven by ledger counts
- [x] ACCEPTANCE 2: a run interrupted after N sources resumes and executes ONLY the remaining sources (asserted on the client's request log)
- [x] tests: both acceptances plus a source-level failure that does not abort the run

DONE WHEN: both acceptances pass against Postgres with recorded fixtures.
EVIDENCE: pasted into docs/evidence/P1.md

## Summary of Changes

The L0 loop, with both acceptances proven by running the real CLI:

```
$ ... --limit 1 --no-resume   status: partial | resumed: False | touched: ['hn_firebase']
$ ... (resume)                status: ok      | resumed: True  | touched: ['arctic_shift', 'wiki_pageviews']
$ ... (again)                 status: ok      | resumed: False | touched: [all three] | new: 0
```

- **Registry order is execution order**; one source failing records the reason and the run
  continues — a broken endpoint must not cost a night's data.
- **Two dedup layers, deliberately distinct**: the payload hash (spec §5.2) skips parsing,
  scoring and the LLM entirely; the signals unique key makes re-parsing an overlapping
  window store nothing. The recorded Reddit windows overlap (a date watermark is coarse),
  which is exactly why both exist: run 2 parsed 67 signals and stored 0; run 3 parsed 0.
- **A source whose plugin is still a stub is skipped with the reason**, not a crash: P1
  implements 3 of 15 L0 sources, and the ledger says so rather than reporting a silent zero.
- **--fixtures replay mode** pins each source's clock to its fixture's recording time and
  honours the recorded item limits, so an offline run makes exactly the recorded requests.
  That is what lets the gate exercise the whole layer with no network.
- **--dry-run writes nothing at all** (the earlier bug where it still wrote the lake is
  fixed and tested), and --limit N leaves a run open on purpose for the crash drill.

### Verification
```
uv run pytest -q  ->  337 passed   (no test touches the network)
```
