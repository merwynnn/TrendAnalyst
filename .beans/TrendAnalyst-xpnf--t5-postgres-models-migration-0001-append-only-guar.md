---
# TrendAnalyst-xpnf
title: T5 — Postgres models + migration 0001 + append-only guards
status: in-progress
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:38:02Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-376u
---

- [ ] store/models.py — the 11 §7 tables, typed SQLAlchemy 2 (Mapped[])
- [ ] store/db.py — engine/session built from settings
- [ ] alembic + migrations/0001_initial (+ CREATE EXTENSION IF NOT EXISTS vector)
- [ ] append-only: scores rejects UPDATE/DELETE; quota_ledger append-only with compare-and-swap
- [ ] tests: upgrade head -> alembic check (zero drift) -> downgrade base -> upgrade again; guard tests reject mutation

DONE WHEN: alembic check reports zero drift and the guards refuse UPDATE/DELETE.
EVIDENCE: pasted into docs/evidence/P0.md
