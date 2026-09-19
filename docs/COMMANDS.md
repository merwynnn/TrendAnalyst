# COMMANDS.md — the full operational command reference

The commands an agent or a human needs, beyond the short list in `AGENT.md` §1. Everything here has
been run on this machine; the output shown is real (trimmed for width, never for truth).

## Health and state

```bash
uv run python -m trend_analyst.health             # human view; exit 0/1/2 = healthy/degraded/down
uv run python -m trend_analyst.health --json      # the same as one JSON document
uv run python -m trend_analyst.monitor.alerts     # alerts with tier, action, reversal, blast radius
uv run python -m trend_analyst.monitor.alerts --json --write   # one document, and append to the outbox
uv run python -m config.cli show                  # layered settings, secrets redacted
uv run python -m config.cli show --env prod       # what prod would run with
uv run python -m config.cli show --set app.log_level=DEBUG     # the CLI layer wins over env and YAML
uv run python -m config.cli registry              # the source registry; file order IS execution order
uv run python -m config.cli registry --registry /path/suspicious.yaml   # validate a file without installing it
```

## The pipeline

```bash
uv run python -m trend_analyst.pipeline.orchestrator --layers L0          # collect (resumes a crashed run)
uv run python -m trend_analyst.pipeline.orchestrator --layers L0,L1,L3    # collect, mine, score, snapshot
uv run python -m trend_analyst.pipeline.orchestrator --rank               # the ranked table, read from snapshots
uv run python -m trend_analyst.pipeline.orchestrator --json               # exactly one JSON document
uv run python -m trend_analyst.pipeline.orchestrator --dry-run            # fetch and parse, write nothing
uv run python -m trend_analyst.pipeline.orchestrator --limit 1            # crash drill: leaves a run unfinished
uv run python -m trend_analyst.pipeline.orchestrator --fixtures tests/data  # offline replay, no network
uv run python -m trend_analyst.pipeline.orchestrator --source hn_firebase   # one source
uv run python -m scripts.nightly                     # L0 -> decide -> judge -> write -> TTL -> reconcile
uv run python -m scripts.nightly --offline --dry-run # the same path on fixtures: no network, no spend
uv run python -m scripts.nightly --skip-judge        # stop after scoring (no provider key needed)
```

### Snapshots and history

```bash
uv run python -m trend_analyst.pipeline.orchestrator --history "<phrase>"          # every snapshot for a candidate
uv run python -m trend_analyst.pipeline.orchestrator --history-delta "<phrase>"    # what moved since the last snapshot
uv run python -m trend_analyst.pipeline.orchestrator --compare-versions <a> <b>    # two snapshot hashes, field by field
```

### The LLM gates (offline by default; live needs a key)

```bash
uv run python -m trend_analyst.pipeline.orchestrator --judge-replay tests/data/llm/judge_verdict.json --fresh
                                       # the Judge gate over a REAL recorded answer: no network, no spend
uv run python -m trend_analyst.pipeline.orchestrator --judge            # live Judge (needs a provider key)
uv run python -m trend_analyst.pipeline.orchestrator --write            # live Writer over the kept candidates
uv run python -m trend_analyst.pipeline.orchestrator --write-replay tests/data/llm/writer_brief.json --fresh
uv run python -m trend_analyst.pipeline.orchestrator --briefs           # stored briefs
uv run python -m trend_analyst.pipeline.orchestrator --brief "circ saw" # one brief's markdown
uv run python -m scripts.llm_drill                   # one live run: failover, cache, accounting, grounding
uv run python -m scripts.judge_replay --help         # drive the Judge over a fixture explicitly
```

## Quality bars

```bash
uv run pytest -m "not db"          # fast path: no database, no network
uv run pytest                      # full suite against local Postgres + pgvector
uv run ruff check .                # lint
uv run mypy src config             # strict types
uv run alembic check               # zero drift between models and migrations
uv run python -m evals.run_evals   # golden cases: expected_keep, fad label, score band
bash scripts/gate.sh               # all of the above, as markdown evidence
uv run python -m scripts.runbook_drill          # the five §9 runbooks against staging
uv run python -m scripts.runbook_drill --keep   # ... leaving the staging database in place
uv run python -m scripts.category_coverage      # how much of the lake the taxonomy matches
uv run python -m scripts.seed_evals --count 10  # seed golden eval cases from real output
```

## Database and migrations

```bash
bash scripts/db_up.sh                 # start Postgres/WSL and park the keep-alive (WSL kills idle distros)
bash scripts/db_down.sh               # terminate the distro (stops ALL WSL work in it)
bash scripts/provision_pg.sh          # idempotent re-provision: role, databases, pgvector, DSN
bash scripts/staging_db.sh create     # the drill's throwaway database (needs the postgres superuser)
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic revision --autogenerate -m "what changed"
TA_MIGRATION_TRACE=1 uv run alembic upgrade head    # where did a migration stop?
# after a schema change ALWAYS: upgrade -> check -> downgrade base -> upgrade (the round trip)
```

## Data capture

```bash
uv run python -m scripts.record_fixtures --source hn_firebase   # re-record a fixture (needs network)
uv run python -m scripts.sync_sources                           # registry vs database; fails loudly
uv run python -m scripts.sync_sources --dry-run
uv run python -m scripts.ttl_job                                # expire raw lake (90d) and LLM cache (30d)
uv run python -m scripts.ttl_job --dry-run                      # what would expire, and what is protected
```

## The results explorer

```bash
uv run python -m trend_analyst.viewer.build --out docs/viewer/index.html
uv run python -m trend_analyst.viewer.build --all-runs      # include older decide runs, not just the newest
```

One self-contained HTML file: no CDN, no framework, no server. Every figure is labelled **stored**
(read from a table), **recomputed** (derived now, from stored inputs) or **pending** (not there yet).

## Task tracking

```bash
beans list --json --ready      # what is ready
beans list --json              # everything, with status
beans show <bean-id>           # one item, with its body
```

The winget-installed `beans` binary needs its path prepended in a fresh shell.
