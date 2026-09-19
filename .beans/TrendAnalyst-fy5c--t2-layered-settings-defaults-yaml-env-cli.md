---
# TrendAnalyst-fy5c
title: 'T2 — Layered settings: defaults < YAML < env < CLI'
status: completed
type: task
priority: high
created_at: 2026-09-19T11:42:42Z
updated_at: 2026-09-19T12:06:11Z
parent: TrendAnalyst-l15l
blocked_by:
    - TrendAnalyst-376u
---

- [x] config/settings.py — pydantic-settings model, nested sections (app, db, llm, tier_a, gates, budgets, health)
- [x] YAML layer: config/settings.yaml + config/settings.<env>.yaml
- [x] secrets layer: config/secrets.local.yaml (untracked, decision B4); template committed as config/secrets.example.yaml
- [x] env layer (optional override, TA_ prefix, __ nesting) + CLI layer (--set section.key=value)
- [x] secret redaction: SecretStr, absent from logs, repr and --json output
- [x] tests: full precedence matrix, redaction, missing-file tolerance, bad-YAML error

DONE WHEN: tests green and a real CLI run proves CLI > env > YAML > defaults.
EVIDENCE: pasted into docs/evidence/P0.md

## Summary of Changes

Layered configuration, with the precedence order proven by both tests and a real CLI run.

- **config/settings.py** — pydantic-settings model: app, db, llm, tier_a, gates, budgets,
  retention, health. Every section is `extra="forbid"` (a typo stops the run) and
  `frozen=True` (configuration is not state). Secrets are SecretStr.
- **Layer order**: field defaults < settings.yaml < settings.<TA_ENV>.yaml
  < secrets.local.yaml < config/.env < env TA_* < CLI --set. The env file is resolved
  per call, so a stray repo-root .env can never leak into a test or a run.
- **Provenance**: describe_sources() reports which layers exist, so the CLI can say what
  it actually read; app.config_dir is forced by the loader and cannot be redirected by YAML.
- **Redaction**: redacted() masks secrets as *** (or "" when unconfigured) and
  secret_status() reports configured-ness only. Tests assert a secret literal never
  appears in repr(), model_dump_json() or the CLI output.
- **Failure modes** are typed (ConfigError) and actionable: missing config dir, unparseable
  YAML, unknown key, non-Postgres DSN, inverted keep-rate band, negative gate budget,
  malformed --set pair.
- **config/cli.py** (`python -m config.cli show [--json] [--env] [--set k=v]`) — the operator
  view. Also wired into scripts/gate.sh as a check that asserts redaction.

### Verification
```
uv run pytest -q          ->  65 passed
uv run ruff check .       ->  All checks passed!
uv run mypy src config    ->  Success: no issues found in 34 source files
uv run python -m config.cli show --json | grep log_level
   no overrides           ->  "DEBUG"    (settings.dev.yaml)
   TA_APP__LOG_LEVEL=ERROR ->  "ERROR"   (environment beats YAML)
   + --set app.log_level=CRITICAL -> "CRITICAL" (CLI beats environment)
   db.url                 ->  "***"     (never the DSN)
```

### Drift found and fixed (caught by the tests)
The model used `db:` while scripts/provision_pg.sh, .env.example and secrets.example.yaml
used `database:`. Renamed everywhere to `db` (and TA_DB__URL), and added a test that fails
if the committed secrets template ever drifts from the model's key set again.
