---
# TrendAnalyst-376u
title: T1 — Scaffold to spec §3 shape + lock the toolchain
status: in-progress
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T11:42:43Z
parent: TrendAnalyst-l15l
---

- [ ] .gitignore protects secrets before the first code commit
- [ ] pyproject.toml: system Python 3.14.6 runtime; ruff + mypy pinned to py312 syntax; deps pinned; uv.lock committed
- [ ] exact §3 tree; later-phase modules are typed stubs that raise NotImplementedError (never a silent no-op)
- [ ] config/ importable as a package (B2a) + committed config/secrets.example.yaml template
- [ ] ruff / mypy --strict / pytest wired; pytest marker: db
- [ ] README.md, AGENT.md, USER_SETUP.md honest stubs (brief §5/§6 bars met in P5)

DONE WHEN: uv sync, ruff, mypy --strict and pytest all clean, and the tree matches §3.
EVIDENCE: raw command output appended to docs/evidence/P0.md
