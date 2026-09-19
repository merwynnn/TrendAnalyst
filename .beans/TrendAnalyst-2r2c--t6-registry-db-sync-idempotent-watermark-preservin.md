---
# TrendAnalyst-2r2c
title: T6 — Registry -> DB sync (idempotent, watermark-preserving)
status: todo
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T11:42:43Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-xpnf
---

- [ ] scripts/sync_sources.py upserts registry entries into the sources table
- [ ] MUST never touch watermarks; re-running is a no-op
- [ ] tests: idempotency, watermark preservation, compare-and-swap semantics

DONE WHEN: two consecutive runs, identical row counts, watermark byte-identical.
EVIDENCE: pasted into docs/evidence/P0.md
