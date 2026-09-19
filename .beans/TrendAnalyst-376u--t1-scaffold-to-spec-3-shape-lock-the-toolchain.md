---
# TrendAnalyst-376u
title: T1 — Scaffold to spec §3 shape + lock the toolchain
status: completed
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:02:07Z
parent: TrendAnalyst-l15l
---

- [x] .gitignore protects secrets before the first code commit
- [x] pyproject.toml: system Python 3.14.6 runtime; ruff + mypy pinned to py312 syntax; deps pinned; uv.lock committed
- [x] exact §3 tree; later-phase modules are typed stubs that raise NotImplementedError (never a silent no-op)
- [x] config/ importable as a package (B2a) + committed config/secrets.example.yaml template
- [x] ruff / mypy --strict / pytest wired; pytest marker: db
- [x] README.md, AGENT.md, USER_SETUP.md honest stubs (brief §5/§6 bars met in P5)

DONE WHEN: uv sync, ruff, mypy --strict and pytest all clean, and the tree matches §3.
EVIDENCE: raw command output appended to docs/evidence/P0.md

## Summary of Changes

Scaffolded the repo to the spec §3 layout and locked the toolchain.

- **Runtime**: the SYSTEM interpreter (3.14.6) builds .venv — no interpreter was
  downloaded (UV_PYTHON_DOWNLOADS=never, UV_NO_MANAGED_PYTHON=1). Spec §11 says 3.12;
  that deviation (D1) is recorded in README and enforced the other way round:
  ruff target-version=py312 and mypy python_version=3.12 reject 3.13+ syntax.
- **Deps pinned exactly** (+ uv.lock): pydantic 2.13.5, pydantic-settings 2.15.0,
  sqlalchemy 2.0.54, psycopg[binary] 3.3.6, alembic, httpx, PyYAML, python-dotenv,
  tzdata; dev: pytest 9.1.1, pytest-cov, ruff 0.16.8, mypy 2.3.1, types-PyYAML.
- **Layout**: every §3 path exists; 20 later-phase modules are stubs that declare
  STUB_PHASE and export nothing. tests/test_layout.py proves the tree, imports every
  module, and fails any stub that could masquerade as working code.
- **Tooling**: scripts/provision_pg.sh (WSL PG 18 + pgvector, writes the DSN into the
  untracked secrets file), scripts/db_up.sh + db_down.sh (WSL terminates idle distros,
  measured — a parked sleep infinity holds it), scripts/gate.sh (markdown evidence).
- **Evidence**: docs/evidence/P0.md — 40 tests pass, ruff clean, mypy strict clean,
  2 checks honestly PENDING (alembic → T5, health CLI → T7).
- **Docs**: README (with the deviation log D1-D5), AGENT.md (commands + boundary tiers;
  runbooks in P5), USER_SETUP.md (live sections: Postgres, credentials, first-run).

### Verification (raw output in docs/evidence/P0.md)
```
bash scripts/gate.sh   ->  GATE: PASS (2 pending)
uv run pytest -q -m 'not db'   ->  40 passed
uv run ruff check .            ->  All checks passed!
uv run mypy src config         ->  Success: no issues found in 32 source files
```
