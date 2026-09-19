---
# TrendAnalyst-2r2c
title: T6 — Registry -> DB sync (idempotent, watermark-preserving)
status: completed
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T14:02:20Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-xpnf
---

- [x] scripts/sync_sources.py upserts registry entries into the sources table
- [x] MUST never touch watermarks; re-running is a no-op
- [x] tests: idempotency, watermark preservation, compare-and-swap semantics

DONE WHEN: two consecutive runs, identical row counts, watermark byte-identical.
EVIDENCE: pasted into docs/evidence/P0.md

## Summary of Changes

`python -m scripts.sync_sources [--dry-run] [--json]` copies config/sources.yaml into the
`sources` table, with the spec's safety properties enforced by construction:

- **The watermark is never written.** The update path iterates an explicit
  REGISTRY_OWNED_COLUMNS list that does not contain `watermark` or
  `watermark_updated_at`: they are runtime state owned by the pipeline (spec §5.1), and a
  configuration sync must not be able to make a source re-fetch or skip work. A test
  changes the registry after setting a watermark and asserts the cursor survives
  byte-identically, and another test asserts the column list itself (a guard for future
  editors).
- **Idempotent**: the second run reports 22 unchanged and does not even move `updated_at`
  (also asserted, because a no-op that still bumps timestamps is not a no-op).
- **Nothing is deleted**: a source that disappears from the registry is disabled in place,
  because signals, raw_items and quota_ledger rows still reference its id. Orphans are
  reported separately (`disabled_missing` vs `already_disabled`) so the operator sees the
  whole picture rather than a count.
- **Dry run** reports exactly which fields would change and writes nothing, so an operator
  can review a registry edit before it lands.
- **The report says what changed, field by field** (`budget_per_day: 10 -> 25`), which is
  what makes --dry-run useful rather than decorative.

### Verification (live, against the dev database)
```
uv run python -m scripts.sync_sources            -> inserted 22, updated 0, unchanged 0
uv run python -m scripts.sync_sources            -> inserted 0, updated 0, unchanged 22
uv run python -m scripts.sync_sources --dry-run  -> 22 unchanged, nothing written
uv run pytest -q                                 -> 216 passed (11 new tests)
uv run ruff check .                              -> All checks passed!
uv run mypy src config scripts                   -> no issues found in 58 source files
```
scripts/gate.sh gained two checks: the sync runs, and its second run is a no-op.
