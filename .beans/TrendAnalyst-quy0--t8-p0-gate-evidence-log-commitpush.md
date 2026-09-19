---
# TrendAnalyst-quy0
title: T8 — P0 gate + evidence log + commit/push
status: todo
type: task
priority: high
created_at: 2026-09-19T11:42:43Z
updated_at: 2026-09-19T11:42:43Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-376u
    - TrendAnalyst-fy5c
    - TrendAnalyst-zai8
    - TrendAnalyst-96uw
    - TrendAnalyst-xpnf
    - TrendAnalyst-2r2c
    - TrendAnalyst-2er7
---

- [ ] pytest -m 'not db' green (no database, no network)
- [ ] full pytest including the db marker green against local pgvector
- [ ] ruff check clean; mypy --strict clean; alembic check zero drift
- [ ] python -m trend_analyst.health on an empty DB reports empty-but-healthy
- [ ] docs/evidence/P0.md contains the raw output of every gate above
- [ ] every bean in the epic up to date; commit and push

DONE WHEN: brief P0 DONE-WHEN is satisfied by evidence, not memory.
EVIDENCE: the gate log itself
