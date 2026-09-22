# Trend Analyst

Discovery and validation engine for underserved product gaps: it scans free public
sources nightly, prunes hard, scores candidates in code (MGS = 0.30·DV + 0.25·(100−SS)
+ 0.20·SP + 0.15·MP + 0.10·FE), and spends LLM calls only at three gates (Planner,
Judge, Writer) with hard budgets.

**The two dossiers in `docs/dossiers/` are the law:**

| Document | Role |
|---|---|
| `1-system-specification.pdf` | Normative. Where it says MUST, this build MUST comply. |
| `2-builder-brief.pdf` | The work plan: phases, standards, definition of done. |
| `product-report.html` | Non-normative product explainer. |

## Status

| Phase | Scope | State |
|---|---|---|
| **P0** | Skeleton, layered config, `sources.yaml` registry (+Tier-A-in-L0 hard-fail), Postgres models + first migration | **done** — see [`docs/evidence/P0.md`](docs/evidence/P0.md) (outcome) and [`P0-gate.md`](docs/evidence/P0-gate.md) (raw gate output) |
| **P1** | L0 collectors + run log, per-source cursors, content-hash dedup | **done** — see [`docs/evidence/P1.md`](docs/evidence/P1.md) (outcome) and [`P1-gate.md`](docs/evidence/P1-gate.md) (raw gate output). Kill-and-resume and the quota ledger were removed in the cleanup; runs are single-shot |
| **P9** | **Source expansion** — 8 more Tier-S collectors live (Mastodon, WordPress, Suggest, Shopify, iTunes, OpenAlex, itch.io) + GDELT plugin written (fixture blocked on their throttle) | **done** — each with recorded fixtures and contract tests; lake grew 3k → 5.5k signals/night. Bluesky (403 block), Google Books (persistent 429) and Common Crawl (URL index can't do full-text) stay documented stubs; Steam skipped per instruction; Product Hunt plugin written, awaiting one live API answer |
| **P2** | L1 mining + scoring v1: per-category EWMA z-scores, 95% prune, MGS in code, fad flag, Monte Carlo revenue, 10 golden eval cases | **done, then replaced** — deterministic n-gram mining was superseded by the LLM Extractor gate (below); scoring, prune economics and evals unchanged. See [`docs/evidence/P2.md`](docs/evidence/P2.md) (outcome) and [`P2-gate.md`](docs/evidence/P2-gate.md) (raw gate output) |
| **P3** | LLM gates: provider chain with failover, per-gate budgets, 30-day cache, Pydantic schemas, code-enforced grounding, the Judge gate and its append-only `judgements` | **done** — see [`docs/evidence/P3.md`](docs/evidence/P3.md) (outcome), [`P3-drill.txt`](docs/evidence/P3-drill.txt) (live drill) and [`P3-gate.md`](docs/evidence/P3-gate.md) (raw gate) |
| **P4** | L2 enrichment (eBay Browse, on-demand, top-K), briefs, snapshot history views, TTL jobs, and the single-shot nightly run (the quota-ledger reconciliation step was removed in the cleanup) | **done** — see [`docs/evidence/P4.md`](docs/evidence/P4.md) (outcome) and [`P4-gate.md`](docs/evidence/P4-gate.md) (raw gate). L2 is unvalidated against live eBay: no credentials yet, and the doc says so |
| **P5** | AGENT.md + USER_SETUP.md to bar, 50 golden evals, CI, monitor dry-run | **done** — `AGENT.md` (5 runbooks, 3 tiers, escalation + rollback), `USER_SETUP.md` (11-key table, 2 schedulers, $0 cost table, 5 failure modes), 50 golden cases, CI + nightly workflows. Evidence: `docs/evidence/P5.md`. (The alert inbox, drift checks and staging runbook drill were removed in the cleanup as later-stage machinery) |
| **P6** | **Results explorer** — one self-contained interactive HTML page per item | **removed** in the cleanup (code + generated HTML deleted; `orchestrator --rank` and `--history` cover the need for now) |
| **P7** | **Top of the funnel** — a candidate must be a product phrase, not a sentence fragment | **done, then replaced** — the head-noun rule, product-form vocabulary, brand blocklist and language gate bought 10/10 product nouns, but a word list cannot keep up with language. The Extractor gate (below) replaces the whole layer; P7's fragment suite survives as its regression cases. Evidence: `docs/evidence/P7.md` |
| **P8** | **Extractor gate** — the LLM reads lake texts in ~30-text chunks and returns grounded products (`phrase, category, doc_ids`); code drops invented refs, ranks by velocity, prunes 95% | **done** — deterministic mining, taxonomy matching and the coverage script deleted; per-model Gemini chain (6 ids) for quota spread; replay deterministic via the 30-day chunk cache. Evidence: `tests/data/llm/extractor_products*.json` + `tests/test_extract.py` |

Gate: `bash scripts/gate.sh` — 16 checks, currently **PASS (0 pending)**. Run it after any
change; it regenerates `docs/evidence/P7-gate.md` with raw output instead of a claim.

Task tracking lives in `beans` (`.beans/`), not in this file: `beans list --ready`.

## Task tracking (beans)

This repo uses [beans](https://github.com/hmans/beans) — a flat-file issue tracker
whose issues are markdown files under `.beans/`, so the task list travels with the code.

```bash
# install (already done on this machine — v0.4.2 via winget)
winget install --id hmans.beans --source winget

# open a NEW terminal after installing: PATH changes do not reach running shells

# day-to-day
beans list                  # the board: epic + tasks, statuses, blockers
beans list --ready          # what can be started right now (unblocked, not in progress)
beans show TrendAnalyst-l15l          # the P0 epic (add more ids to see several)
beans show TrendAnalyst-376u --json   # machine-readable, for agents
beans tui                   # interactive UI
beans prime                 # the canonical usage guide for coding agents

# moving work along
beans update <id> -s in-progress      # exactly one bean in progress at a time
beans update <id> -s completed
beans create "Title" -t task -d "body" -s todo
```

On this machine the binary lives at
`%LOCALAPPDATA%\Microsoft\WinGet\Packages\hmans.beans_Microsoft.Winget.Source_8wekyb3d8bbwe\beans.exe`
(winget put that directory on the user PATH). To use it from a terminal that was
already open when it was installed:

```powershell
& "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\hmans.beans_Microsoft.Winget.Source_8wekyb3d8bbwe\beans.exe" list
```

## Quickstart (development)

```bash
# 1. Postgres 18 + pgvector, inside WSL (idempotent; also writes config/secrets.local.yaml)
wsl -d Ubuntu -u root -- bash scripts/provision_pg.sh

# 2. Python environment — uses the SYSTEM interpreter 3.14.6, never downloads one
uv sync                      # UV_PYTHON_DOWNLOADS=never UV_NO_MANAGED_PYTHON=1

# 3. Gates
uv run pytest                # full suite (needs Postgres)
uv run pytest -m "not db"    # no database, no network
uv run ruff check .
uv run mypy src config

# 4. Smoke test — the whole pipeline on fixtures, then the ranked table
uv run python -m scripts.nightly --offline --dry-run
uv run python -m trend_analyst.pipeline.orchestrator --rank
```

Credentials live in `config/secrets.local.yaml` (untracked, template:
`config/secrets.example.yaml`). Environment variables are an optional *override*
layer, used by CI — never the place secrets are kept.

## Layout

`src/trend_analyst/` follows the spec (§3): `pipeline/` (+ `runs.py`, `layers/`),
`sources/` (+ `tier_s/`, `tier_a/`), `scoring/`, `llm/`, `store/`.
(`monitor/` and the results `viewer/` were removed in the cleanup.)
Modules that belong to a later phase exist now as **typed stubs that document their
phase and export nothing** — never as silent no-ops.

## Dev-diation log (deliberate, approved)

Deviations from the spec are recorded here rather than silently taken. Each is a
decision the human approved or an addition the spec layout does not name.

| # | Deviation | Reason |
|---|---|---|
| D1 | Runtime interpreter is the **system Python 3.14.6**, not 3.12 (spec §11) | Human instruction: "use the python installed on the system, do not install another python." Code stays 3.12-portable: `ruff target-version = py312` and `mypy python_version = 3.12` reject 3.13+ syntax. |
| D2 | `config/` is an importable package (`config/settings.py` per spec §3 lives outside `src/`) | Decision B2a — keeps every spec path while allowing type-checked imports. |
| D4 | `src/trend_analyst/logging.py`, `config/cli.py`, `docs/`, `alembic.ini`, `.github/`, `tests/*/` subpackages | The spec layout has no home for structured logging, a config inspector, evidence logs, migrations config or CI. Additions only. |
| D5 | Dossiers moved from the repo root to `docs/dossiers/` | Spec §3: "nothing ad-hoc at root". |
| D6 | `config/secrets.local.yaml` is the credential store (env vars remain an optional override) | Human instruction B4: "don't put the api keys on environment variable, let them in a file". Spec §10 allows either. |
| D7 | Postgres runs on Neon cloud (PG 18 + pgvector), not in WSL | Superseded September 2026: the WSL cluster (Ubuntu 26.04, PG 18) kept dying on idle shutdown and taking the localhost relay with it. Native Windows PG was rejected as too heavy; pgvector ships no upstream Windows binaries (and nothing here uses vector columns anyway). Neon is zero-install with pgvector included — only the DSN changed. The WSL scripts (`provision_pg.sh`, `db_up.sh`) and cluster remain as fallback, not the path. |
| D9 | `src/trend_analyst/net.py` — the HTTP client, egress allowlist and fixture replay | The spec layout has no home for network plumbing; plugins must not choose their own transport (spec §10) |
| D12 | `judgements` table (migration 0002) — §6.2's cited keep/drop verdicts have no home in the §7 table list, and the LLM cache expires in 30 days, so the advice would die while the decision it justified lived on | The Judge's output is durable, append-only and queryable, with its citations and token counts |
| D14 | `judgements` gets a table (D12) and the `HttpClient` contract gained a `post` method | eBay's OAuth token endpoint is a POST, and the plugin contract was written for GET-only sources. A fake GET token endpoint would have been pinned by a fixture and exposed by the first live run |
| D15 | `store/history.py` and `store/ttl.py` (spec §3 lists neither) | §5.3 requires the snapshot table to be *read* for trends and §5.4 requires a TTL job; the layout has no home for either. `history.py` reads, `ttl.py` expires, and both live in `store/` with the tables they describe |
| D13 | A second Gemini model in the provider chain | The live drill produced a real 503 "experiencing high demand" on the first model while small requests succeeded, and with Groq unkeyed and Cerebras unfunded (402) one model per provider meant a gate outage. The distinct-provider order is still Gemini → Groq → Cerebras → Ollama |
| D18 | `evals/run_evals.py --relabel` as the only supported way to change a golden case's label | Seeding 50 cases surfaced two label conflicts where the Judge's reasoning was better than the phrase-quality baseline's (an n-gram fragment scoring above the keep threshold). A label must be changeable — silently editing `cases.yaml` would hide the disagreement — so moving one demands `--to` and `--reason`, and the reason is written into the case's `notes` |
| D10 | `src/trend_analyst/store/sync.py` — registry mirror logic moved out of `scripts/` | The orchestrator needs it before a run (a cursor lives on the source row), and `src/` must not import from `scripts/` |
| D19 | The monitor (`monitor/`, health CLI, alert inbox, staging drill), the results viewer, the quota ledger with its resume/CAS machinery, and the runbook drill are deleted | Later-stage machinery the current pipeline does not need. Runs are single-shot; per-source cursors are the only cross-run memory. Spec §3 layout entries for the deleted modules no longer apply |
| D20 | L1 is an LLM gate (Extractor), not deterministic code | N-gram mining produced fragments more often than products (P7: 67% of Judge drops) and the taxonomy silently deleted real finds. The gate reads ~30-text chunks under per-gate budgets, refs are grounded in code, velocity/prune/scoring stay deterministic, and `--fixtures`/`--offline` run on an honest heuristic stand-in. New code in `llm/extract.py`; `llm_cache` CHECK widened (migration 0004); chain spread over per-model Gemini quotas (live-probed) |
| D8 | The DSN uses `127.0.0.1`, never `localhost` | On Windows `localhost` resolves to IPv6 `::1` FIRST, and WSL's port relay black-holes `::1`: every connection then burned 130 s before falling back to IPv4 (measured: 130.09 s as `localhost`, 0.06 s as `127.0.0.1`). Same reason `migrations/env.py` sets `connect_timeout` and `lock_timeout` — a migration must fail fast, never hang. |

## Rules of the road

- **Spec is law.** Where spec and instinct conflict, follow the spec and flag it.
- **Verify by running.** No claim of "green" from memory; raw output goes to `docs/evidence/`.
- **No secrets** in code, logs, fixtures, snapshots, or docs. Tests use recorded fixtures with secrets redacted.
- **No live network in tests.** Ever.
- **One bean in progress at a time.** `beans list --ready`.
