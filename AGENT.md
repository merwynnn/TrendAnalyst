# AGENT.md — operating manual for the monitor agent

> **Bar:** brief §5. Sections marked `TODO(P5)` are completed in phase P5, after the
> runbooks they describe actually exist. This file describes reality, not wishes.

## 1. Commands (run these first, in this order)

```bash
# Health — ALWAYS the first command. Reads the health CLI, never raw logs.
uv run python -m trend_analyst.health            # human view
uv run python -m trend_analyst.health --json      # machine view (parse this)
echo $?                                           # 0 healthy / 1 degraded / 2 down

# What is the system ACTUALLY configured with? (secrets always redacted)
uv run python -m config.cli show
uv run python -m config.cli show --json --env prod          # what prod would run with
uv run python -m config.cli show --set app.log_level=DEBUG  # CLI layer beats env/YAML

# The source registry: file order IS execution order. Fails loudly on a bad registry.
uv run python -m config.cli registry
uv run python -m config.cli registry --json
uv run python -m config.cli registry --registry /path/to/suspicious.yaml   # validate a file

# What was I doing / what is left
beans list --json --ready
beans show <bean-id>

# Gates (must all pass before you report a change as done)
uv run pytest -m "not db"     # no database needed
uv run pytest                 # full suite, needs local Postgres
uv run ruff check .
uv run mypy src config
bash scripts/gate.sh          # all of the above, as markdown evidence

# Local Postgres (WSL). WSL kills idle distros, so db_up.sh parks a keep-alive:
# a bare `pg_ctlcluster start` will look healthy and then vanish.
bash scripts/db_up.sh            # idempotent; waits for 127.0.0.1:5432 to answer
bash scripts/db_down.sh          # terminates the distro (stops ALL WSL work in it)
wsl -d Ubuntu -u root -- pg_lsclusters
bash scripts/provision_pg.sh     # idempotent re-provision (role, database, pgvector)

# Migrations
uv run alembic upgrade head
uv run alembic check          # zero drift between models and migrations
uv run alembic downgrade -1
TA_MIGRATION_TRACE=1 uv run alembic upgrade head   # where did a migration stop?

# After a schema change: uv run alembic revision --autogenerate -m "what changed"
# Then ALWAYS: upgrade -> check -> downgrade base -> upgrade (the round trip)

# Run L0 (resumes a crashed run automatically; one source failing never stops the rest)
uv run python -m trend_analyst.pipeline.orchestrator --layers L0
uv run python -m trend_analyst.pipeline.orchestrator --json            # machine-readable
uv run python -m trend_analyst.pipeline.orchestrator --dry-run         # fetch and parse, write nothing
uv run python -m trend_analyst.pipeline.orchestrator --limit 1         # crash drill: leave a run open
uv run python -m trend_analyst.pipeline.orchestrator --fixtures tests/data   # offline replay, no network
```

## 2. Project structure and stack

```
config/            settings.py (layered config), sources.yaml (registry = execution order),
                   categories.yaml, secrets.local.yaml (UNTRACKED — never read it into a log)
src/trend_analyst/ pipeline/ (orchestrator, layers/, state) · sources/ (base, registry, tier_s/, tier_a/)
                   scoring/ (features, normalize, mgs, fad, revenue) · llm/ (gateway, cache, gates, schemas)
                   store/ (db, models, snapshots) · monitor/ (health, alerts, drift)
evals/             golden cases + runner        tests/  pytest (no live network)
migrations/        alembic                     scripts/ operational scripts
```

Python (spec §11 lists 3.12; this machine runs the **system 3.14.6** — README D1) ·
PostgreSQL 18 + pgvector (WSL) · SQLAlchemy 2 + Alembic · pydantic v2 /
pydantic-settings · httpx · pytest + ruff + mypy --strict.

Logs are structured JSON with `run_id` on every line. Alerts land in the local
outbox file the monitor agent triages; they never leave the machine.

## 3. Boundaries — what you may do alone

| Tier | Contents |
|---|---|
| **Always do** | Read health output first; append to logs; restart a failed run from checkpoint; clear cache entries older than TTL; report exactly what you did, with the command output |
| **Ask first** (file a proposal, then wait) | Enable/disable a source; change thresholds, weights or budgets; add a dependency; modify the CI schedule; touch migrations; anything that increases spend |
| **Never do** | Commit secrets; delete snapshots, ledger rows or eval cases; edit `.env` or production config; force-push; bulk-delete raw lake rows; approve your own proposal |

## 4. Monitor loop

1. Read health (`--json`). 2. Classify: `healthy` / `degraded` / `down`.
3. Match a runbook. 4. Act **only** within the tier above.
5. Verify with the runbook's verification step. 6. Log the action + its rollback command.
7. Escalate if no runbook matches, or if the action is approval-class.

## 5. Runbooks

`TODO(P5)` — the five required runbooks are written in P5 once each failure mode is
reproducible against staging:

1. HTTP 429 storm from a source
2. Source approval / schema change breaking a plugin
3. Quota burn above 80 %
4. Judge keep-rate drift outside band
5. Eval baseline drop

Until they exist: **take no corrective action beyond the "Always do" tier.**

## 6. Change classes

| Class | Example | Verification step |
|---|---|---|
| SAFE | Restart a run from its checkpoint | Health shows the run resuming only unfinished work; no double quota spend in `quota_ledger` |
| APPROVAL | Disable a flaky source for 7 days | Proposal filed, human approved; registry change is one revert-able commit |
| FORBIDDEN | Editing snapshot rows to fix a bug | — (append a correction row instead) |

## 7. Escalation template

```
SYMPTOM:      one line
EVIDENCE:     the commands you ran + their output
ATTEMPTED:    safe actions already taken (with results)
PROPOSED:     the approval-class change, and its blast radius if ignored
ROLLBACK:     the single command that reverses whatever you changed
```

## 8. Rollback rule

Every change you make MUST be reversible with **one command**, and that command MUST
be stated in the log line you write. If you cannot state it, you cannot make the change.
