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
| **P0** | Skeleton, layered config, `sources.yaml` registry (+Tier-A-in-L0 hard-fail), Postgres models + first migration, health CLI | **done** — see [`docs/evidence/P0.md`](docs/evidence/P0.md) (outcome) and [`P0-gate.md`](docs/evidence/P0-gate.md) (raw gate output) |
| **P6** | **Results explorer** — one self-contained interactive HTML page with maximum detail per item: every named source that mentioned it with quotes and URLs, all five sub-scores with their inputs, the full feature vector, the fad features, the Monte Carlo assumptions, score history per weights version, run/gate status, and an explicit "still missing" panel for the phases that have not run | **first cut done** — `uv run python -m trend_analyst.viewer.build --out docs/viewer/index.html`; deep-link an item with `#item=<phrase>`. Extends as P3 (Judge verdicts, cited quotes), P4 (Tier-A market data) and P5 (Writer briefs, alerts, eval results) land |
| **P4** | L2 enrichment (eBay Browse, on-demand, top-K), briefs, snapshot history views, TTL jobs, and the nightly run with reconciliation to zero unexplained spend | **done** — see [`docs/evidence/P4.md`](docs/evidence/P4.md) (outcome) and [`P4-gate.md`](docs/evidence/P4-gate.md) (raw gate). L2 is unvalidated against live eBay: no credentials yet, and the doc says so |
| **P3** | LLM gates: provider chain with failover, per-gate budgets, 30-day cache, Pydantic schemas, code-enforced grounding, the Judge gate and its append-only `judgements` | **done** — see [`docs/evidence/P3.md`](docs/evidence/P3.md) (outcome), [`P3-drill.txt`](docs/evidence/P3-drill.txt) (live drill) and [`P3-gate.md`](docs/evidence/P3-gate.md) (raw gate) |
| **P2** | L1 mining + scoring v1: phrase extraction, per-category EWMA z-scores, 95% prune, MGS in code, fad flag, Monte Carlo revenue, 10 golden eval cases | **done** — see [`docs/evidence/P2.md`](docs/evidence/P2.md) (outcome) and [`P2-gate.md`](docs/evidence/P2-gate.md) (raw gate output) |
| **P1** | L0 collectors (HN, Wikipedia, Arctic Shift) + run ledger, watermarks, content-hash dedup, kill-and-resume | **done** — see [`docs/evidence/P1.md`](docs/evidence/P1.md) (outcome) and [`P1-gate.md`](docs/evidence/P1-gate.md) (raw gate output) |
| P2 | L1 mining + scoring v1 (MGS, fad features, Monte Carlo revenue) | not started |
| P3 | LLM gates, gateway, cache | not started |
| P4 | L2 enrichment, briefs, snapshots, TTL jobs | not started |
| P5 | AGENT.md + USER_SETUP.md to bar, 50 golden evals, CI, monitor dry-run | not started |

Gate: `bash scripts/gate.sh` — 16 checks, currently **PASS (0 pending)**. Run it after any
change; it regenerates `docs/evidence/P4-gate.md` with raw output instead of a claim.

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

# 4. Health (spec §9)
uv run python -m trend_analyst.health
uv run ta-health --json
```

Credentials live in `config/secrets.local.yaml` (untracked, template:
`config/secrets.example.yaml`). Environment variables are an optional *override*
layer, used by CI — never the place secrets are kept.

## Layout

`src/trend_analyst/` follows the spec (§3) exactly: `pipeline/` (+ `layers/`),
`sources/` (+ `tier_s/`, `tier_a/`), `scoring/`, `llm/`, `store/`, `monitor/`.
Modules that belong to a later phase exist now as **typed stubs that document their
phase and export nothing** — never as silent no-ops.

## Dev-diation log (deliberate, approved)

Deviations from the spec are recorded here rather than silently taken. Each is a
decision the human approved or an addition the spec layout does not name.

| # | Deviation | Reason |
|---|---|---|
| D1 | Runtime interpreter is the **system Python 3.14.6**, not 3.12 (spec §11) | Human instruction: "use the python installed on the system, do not install another python." Code stays 3.12-portable: `ruff target-version = py312` and `mypy python_version = 3.12` reject 3.13+ syntax. |
| D2 | `config/` is an importable package (`config/settings.py` per spec §3 lives outside `src/`) | Decision B2a — keeps every spec path while allowing type-checked imports. |
| D3 | `src/trend_analyst/health.py` exists alongside `monitor/health.py` | Spec conflict C1: §3 puts health in `monitor/`, §9 mandates `python -m trend_analyst.health`. The shim re-exports; both hold. |
| D4 | `src/trend_analyst/logging.py`, `config/cli.py`, `docs/`, `alembic.ini`, `.github/`, `tests/*/` subpackages | The spec layout has no home for structured logging, a config inspector, evidence logs, migrations config or CI. Additions only. |
| D5 | Dossiers moved from the repo root to `docs/dossiers/` | Spec §3: "nothing ad-hoc at root". |
| D6 | `config/secrets.local.yaml` is the credential store (env vars remain an optional override) | Human instruction B4: "don't put the api keys on environment variable, let them in a file". Spec §10 allows either. |
| D7 | Postgres runs in WSL (Ubuntu 26.04, PG 18 + pgvector) and is held up by `scripts/db_up.sh` | Human decision B1a. WSL terminates idle distros (measured), so a parked `sleep infinity` keeps it reachable — the alternative, a native Windows build, cannot install pgvector without an MSVC toolchain. |
| D9 | `src/trend_analyst/net.py` — the HTTP client, egress allowlist and fixture replay | The spec layout has no home for network plumbing; plugins must not choose their own transport (spec §10) |
| D12 | `judgements` table (migration 0002) — §6.2's cited keep/drop verdicts have no home in the §7 table list, and the LLM cache expires in 30 days, so the advice would die while the decision it justified lived on | The Judge's output is durable, append-only and queryable, with its citations and token counts |
| D14 | `judgements` gets a table (D12) and the `HttpClient` contract gained a `post` method | eBay's OAuth token endpoint is a POST, and the plugin contract was written for GET-only sources. A fake GET token endpoint would have been pinned by a fixture and exposed by the first live run |
| D15 | `store/history.py` and `store/ttl.py` (spec §3 lists neither) | §5.3 requires the snapshot table to be *read* for trends and §5.4 requires a TTL job; the layout has no home for either. `history.py` reads, `ttl.py` expires, and both live in `store/` with the tables they describe |
| D13 | A second Gemini model in the provider chain | The live drill produced a real 503 "experiencing high demand" on the first model while small requests succeeded, and with Groq unkeyed and Cerebras unfunded (402) one model per provider meant a gate outage. The distinct-provider order is still Gemini → Groq → Cerebras → Ollama |
| D11 | The results explorer lives in `src/trend_analyst/viewer/` and writes one self-contained HTML file | The spec's §3 tree has no viewer, and "new code goes in the matching folder, nothing ad-hoc at root" leaves the package as its home. A served app would need auth, a port and a supervisor to answer a question a file answers |
| D10 | `src/trend_analyst/store/sync.py` — registry mirror logic moved out of `scripts/` | The orchestrator needs it before a run (a watermark lives on the source row), and `src/` must not import from `scripts/` |
| D8 | The DSN uses `127.0.0.1`, never `localhost` | On Windows `localhost` resolves to IPv6 `::1` FIRST, and WSL's port relay black-holes `::1`: every connection then burned 130 s before falling back to IPv4 (measured: 130.09 s as `localhost`, 0.06 s as `127.0.0.1`). Same reason `migrations/env.py` sets `connect_timeout` and `lock_timeout` — a migration must fail fast, never hang. |

## Rules of the road

- **Spec is law.** Where spec and instinct conflict, follow the spec and flag it.
- **Verify by running.** No claim of "green" from memory; raw output goes to `docs/evidence/`.
- **No secrets** in code, logs, fixtures, snapshots, or docs. Tests use recorded fixtures with secrets redacted.
- **No live network in tests.** Ever.
- **One bean in progress at a time.** `beans list --ready`.
