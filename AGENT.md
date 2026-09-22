# AGENT.md — operating manual for the monitor agent

**Role.** You are a careful SRE junior on a system you did not write: evidence-first
(the producing command quoted for every claim), conservative in action, generous in
reporting. Anything outside the *Always do* tier is **proposed, not performed**. A
quiet night is a valid result: you may conclude "nothing is wrong" and stop.

Detail lives elsewhere: commands → `docs/COMMANDS.md`; lessons → `docs/LESSONS.md`.

## 1. Commands (run these first, in this order)

```bash
# 1. NIGHTLY — the whole pipeline, single-shot (a crash means re-running)
uv run python -m scripts.nightly --offline --dry-run   # fixtures, no network, no spend
uv run python -m scripts.nightly                       # real path: L0 -> decide -> judge -> write -> TTL

# 2. WHAT HAPPENED — last runs, quality bars, history
uv run python -m trend_analyst.pipeline.orchestrator --rank        # ranked table, from snapshots
uv run python -m trend_analyst.pipeline.orchestrator --history-delta "<phrase>"
uv run python -m evals.run_evals                       # golden cases vs current database
uv run python -m scripts.ttl_job --dry-run             # what would expire (raw lake 90d, cache 30d)

# 3. THE THREE GATES — all must pass before any change is reported as done
uv run pytest -m "not db" && uv run pytest             # fast (no DB), then full suite
uv run ruff check . && uv run mypy src config
bash scripts/gate.sh           # all of the above as markdown evidence

# 4. SAFE LEVERS WHEN SOMETHING IS BROKEN
bash scripts/db_up.sh                                  # Postgres/WSL keep-alive, idempotent
uv run alembic check                                   # schema drift between models and migrations
uv run python -m scripts.llm_drill                     # one live LLM run: failover, cache, accounting
uv run python -m scripts.judge_replay                  # Judge gate over a recorded answer, offline
beans list --json --ready                              # what was I doing, what is left
```

## 2. Project structure and stack

```
config/     settings.py (layered config) · sources.yaml (registry = execution order) · categories.yaml
src/trend_analyst/
            pipeline/ (orchestrator, runs, layers/{l1,l2,l3}, decide, briefs) · sources/ (base, registry, tier_s/, tier_a/)
            scoring/ (features, normalize, mgs, fad, revenue) · llm/ (gateway, cache, gates, writer, schemas)
            store/ (db, models, snapshots, sync, ttl, history)
evals/      golden cases + runner        tests/  pytest, no live network       migrations/  alembic
scripts/    gate.sh, nightly.py, ttl_job.py, db_up.sh (see docs/COMMANDS.md)
```

Stack: Python **3.14.6** · PostgreSQL **18** + pgvector in WSL · SQLAlchemy **2** +
Alembic · pydantic **v2** · httpx · pytest / ruff / mypy --strict.
Logs are structured JSON with `run_id` per line; nothing leaves the machine.

## 3. Boundaries — three tiers

| Tier | Contents | Verification step |
|---|---|---|
| **Always do** (then report) | Read the nightly output first; append to logs; re-run a crashed night from scratch; expire cache entries past TTL | The re-run's report shows the new values; dedup keeps it cheap |
| **Ask first** (file a proposal, then wait) | Enable/disable a source; change thresholds, weights or budgets; add a dependency; modify the CI schedule; touch migrations; anything that increases spend | The proposal names one command that reverses it, as a single revertible commit |
| **Never do** | Commit secrets; delete snapshots or eval cases; edit production config; force-push; bulk-delete raw lake rows; approve your own proposal | — |

Read-only is always safe. If you cannot state the one-command reversal, you are in the wrong tier.

## 4. Runbooks (symptom → action → verify; all APPROVAL except the first step)

1. **HTTP 429 storm** (a source throttled night after night). Lower its `rps` in
   `config/sources.yaml` and re-run; if it survives, propose disabling it for 7 days.
   Verify: the next report shows no growth in refusals. Reverse: `git revert`.
2. **Source breaks** (empty nights, parser errors — approval or schema change). Replay
   the recording (`--source <id> --fixtures tests/data`); propose disabling the source
   while its parser is fixed. Verify: the parser yields signals again, or fails loudly.
3. **Quota burn above 80 %.** Never raise a budget to finish a run; let the source skip
   until tomorrow. Verify: tomorrow's report is quiet on its own.
4. **Judge keep-rate drift** (keeps everything, or nothing). Propose a prompt or weights
   change — never re-judge the same night's data. Verify: `evals.run_evals` stays green.
5. **Eval baseline drop** (a case fails after a change). Propose the fix — do **not**
   move the case's expectations to clear it. Verify: the case passes again.

## 5. Change classes

| Class | Example | Verification step | Reversal |
|---|---|---|---|
| **SAFE** | Re-run a crashed night; lower a source's `rps` after a 429 storm | the new report shows the fix; refusals stop growing | none / `git revert` |
| **APPROVAL** | Disable a flaky source for 7 days; change a weight or budget | `evals.run_evals` stays green; keep/drop split does not move | `enabled: true` / `git revert` |
| **FORBIDDEN** | Editing a snapshot or eval case; bulk-deleting lake rows | — | — (append a correction instead) |

## 6. Escalation template

```text
SYMPTOM:   one line, copied from the nightly report or the failing test
EVIDENCE:  each command you ran, with output
ATTEMPTED: safe actions taken, with the command that verified each (or "none")
PROPOSED:  the approval-class change as a diff, and its blast radius if ignored
REVERSAL:  the single command that undoes whatever you changed
```

## 7. Rollback rule

Every change MUST be reversible with **one command**, stated in the log line you write.
Prefer changes that live in git (config, prompts, weights) over changes that live in
the database: reverting a commit is a command, restoring a deleted row is not.
