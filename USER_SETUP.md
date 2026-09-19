# USER_SETUP.md — the human action list

Everything a person has to do, in order, to get the Trend Analyst running on their own machine. No
step requires reading code. If you can follow this file and reach a green health check, the setup
worked; if you cannot, this file is wrong and should be fixed before anything else.

**The whole system runs at $0/month at the volume in §5.** Two things cost money if you go past that
volume: LLM tokens and Tier-A API calls. Both are capped in code before they can spend (§5's table
says where each cap is).

**Time:** about 20 minutes to a green health check, plus waiting on any approvals you choose to file.

---

## 1. Local Postgres — first, free, no account

| Step | Command | Expected |
|---|---|---|
| Install + start PostgreSQL 18 with pgvector, create the roles/databases, write the DSN | `wsl -d Ubuntu -u root -- bash scripts/provision_pg.sh` | Ends with `[provision_pg] OK`; reports the cluster `18/main online` and `pgvector` |
| Start / stop the database by hand | `wsl -d Ubuntu -u root -- pg_ctlcluster 18 main start` \| `stop` | `pg_lsclusters` shows `online` / `down` |
| Keep the cluster alive | `bash scripts/db_up.sh` | Idempotent; parks a keep-alive process (WSL2 shuts an idle distro down, taking Postgres with it) |

The script creates two databases: `trend_analyst` (yours) and `trend_analyst_test` (the test suite's,
so a test can migrate up and down without ever touching your data). The DSN it writes goes into
`config/secrets.local.yaml`, which is untracked and never committed.

Nothing is exposed outside this machine: Postgres listens on WSL's localhost, which Windows reaches
at `127.0.0.1:5432`. Use the address literal, never `localhost` — on Windows `localhost` prefers IPv6
`::1`, which WSL does not forward, and every connection then waits ~130 s before falling back.

## 2. Credentials — one row per key

Keys live in **`config/secrets.local.yaml`** (untracked). Create it once:

```bash
cp config/secrets.example.yaml config/secrets.local.yaml   # then paste keys by hand
```

Every key is **optional**. With none of them, the system runs: collection, mining, scoring, the
ranking, the snapshot history and the viewer all work from keyless Tier-S sources. A missing key makes
its source *skip with a reason* — it never looks like zero results.

| Credential | Purpose | Where to create it | Free tier | Key in `secrets.local.yaml` | Verify command → expected |
|---|---|---|---|---|---|
| **Gemini API key** | LLM gateway, primary (Judge, Writer) | `aistudio.google.com/apikey` | free tier; RPM/RPD limited — 429s are normal at volume | `llm.gemini_api_key` | `uv run python -m scripts.llm_drill` → `DRILL: PASS` with 5 probes |
| **Groq API key** | gateway failover #2 | `console.groq.com/keys` | free tier | `llm.groq_api_key` | same drill; the failover probe names which provider answered |
| **Cerebras API key** | gateway failover #3 | `cloud.cerebras.ai` | free tier | `llm.cerebras_api_key` | same drill |
| **Ollama** (optional) | gateway last resort, runs locally | `ollama.com/download` | free, local | `llm.ollama_base_url` | `curl http://127.0.0.1:11434/api/tags` → JSON listing models |
| **eBay App ID + Cert** | Tier-A: active supply and price spread | `developer.ebay.com` → Application Keys | 5,000 calls/day (production keyset) | `tier_a.ebay_client_id`, `tier_a.ebay_client_secret` | `uv run python -m trend_analyst.pipeline.orchestrator --layers L2 --json` → `ebay: ok` and a listings count |
| **GitHub PAT** | Tier-A: star/fork velocity | `github.com/settings/tokens` (fine-grained, public read) | 5,000 requests/hour | `tier_a.github_token` | same command → `github: ok` |
| **Best Buy API key** | Tier-A: trending electronics | `developer.bestbuy.com` | free key | `tier_a.bestbuy_api_key` | same command → `bestbuy: ok` |
| **Serper API key** | Tier-A: programmable SERP / People-Also-Ask | `serper.dev` | 2,500 free queries | `tier_a.serper_api_key` | same command → `serper: ok` |
| **Product Hunt token** | Tier-A: launch velocity | `producthunt.com/v2/oauth/applications` | free | `tier_a.producthunt_token` | same command → `producthunt: ok` |
| **Walmart keys** | Tier-A: mass-market trending | `developer.walmart.com` | free with an affiliate account | `tier_a.walmart_client_id`, `tier_a.walmart_client_secret` | same command → `walmart: ok` |
| **SearchAPI key** | Tier-A: ad-library sampling | `searchapi.io` | 100 free requests | `tier_a.searchapi_key` | same command → `searchapi: ok` |

Tier-A keys make rows appear and can cost money past their free tier. Tier-S sources need **no key at
all** and are where the ranked table comes from.

## 3. Accounts and approvals to file on day one

The build never blocks on an approval: every source that needs one ships `enabled: false` in
`config/sources.yaml` and stays that way until its key exists.

| File this | Expected wait | What to do meanwhile |
|---|---|---|
| eBay developer account → **production** keyset (the sandbox keyset is not the dataset) | hours to days | Tier-A stays disabled. L0 + L1 already produce the ranked table from Tier-S sources |
| Walmart affiliate account → API keys | days to weeks | ditto — this one is the slowest; file it first if you care about it |
| Product Hunt developer token | minutes | ditto |
| SearchAPI account | minutes | ditto |
| Reddit API (only if you want live Reddit instead of the Arctic Shift archive) | **weeks**, often refused | Nothing: Arctic Shift already supplies the same subreddits, keyless |

## 4. First run — the checklist, in order

```bash
# 1. database (once; idempotent, so re-running is safe)
wsl -d Ubuntu -u root -- bash scripts/provision_pg.sh

# 2. python environment — uses the system interpreter and downloads no Python
export UV_PYTHON_DOWNLOADS=never UV_NO_MANAGED_PYTHON=1
uv sync

# 3. schema, then prove the schema matches the models
uv run alembic upgrade head
uv run alembic check          # expected: "No new upgrade operations detected."

# 4. the gates (no database needed for the first one)
uv run pytest -m "not db"     # expected: "337 passed, ... deselected"
uv run pytest                 # expected: all green (needs Postgres from step 1)

# 5. collect something real (keyless Tier-S sources; a few minutes)
uv run python -m trend_analyst.pipeline.orchestrator --layers L0

# 6. mine and score, then read the ranking
uv run python -m trend_analyst.pipeline.orchestrator --layers L1,L3
uv run python -m trend_analyst.pipeline.orchestrator --rank

# 7. the acceptance test for setup
uv run python -m trend_analyst.health
echo "exit=$?"                 # expected: exit=0
```

**Healthy output looks like this** (after step 6; the numbers depend on how much you collected):

```
Trend Analyst health — HEALTHY (exit 0)
checked at 2026-09-19T20:12:44+00:00

database: ok — postgres 18.6, pgvector 0.8.1
registry: ok — 22 sources (15 Tier S, 7 Tier A) · 15 enabled, 7 disabled
last run: ok — nightly, finished 2026-09-19T20:11:02+00:00
sources: 3 ok · 0 degraded · 0 down · 12 never_run · 7 disabled
quota: 980 / 22400 requests today (4.4%), alert at 80%
gates: judge keep-rate n/a — no Judge decisions recorded yet
evals: 50 case(s) — 0 verified, 50 partial (keep/drop pending a Judge verdict)
```

Reading it: `never_run` means a source is configured and leashed but has not fetched yet — normal
before its first run. `gates` and the eval line say what has *not* been checked yet rather than
implying success: with no LLM key, no candidate has been judged, and every eval case is `partial`
(the deterministic checks pass, the keep/drop check is pending) instead of "green".

Add `--verbose` to list every source, or `--json` for the machine-readable report the monitor agent
parses. If any line reads `DOWN`, or the exit code is not 0, jump to §6.

## 5. Scheduler and cost

**What to enable.** Two schedules, deliberately different jobs:

1. **The local nightly — the real one.** It is the only place live sources and LLM gates can run,
   because it is the only place with keys. WSL's cron:

   ```bash
   # inside WSL (wsl -d Ubuntu):
   crontab -e
   # add exactly this line — 03:07 local time, log appended, never overlapping itself:
   7 3 * * * cd /mnt/c/Users/Louis/Documents/Projects/TrendAnalyst && flock -n /tmp/ta-nightly.lock bash -lc 'export UV_PYTHON_DOWNLOADS=never UV_NO_MANAGED_PYTHON=1; bash scripts/db_up.sh && uv run python -m scripts.nightly >> logs/nightly.log 2>&1'
   ```

   Manual run of the same thing: `uv run python -m scripts.nightly` (add `--offline --dry-run` to
   rehearse on fixtures without network or spend, or `--skip-judge` to stop before the LLM gates).

2. **The CI nightly — the reproducible one** (`workflow_dispatch` enabled, so it can also be run
   by hand). `.github/workflows/nightly.yml` runs every night at 03:07 UTC on GitHub's runners: a real Postgres+pgvector service, the committed migrations, every
   layer over recorded fixtures, the ledger reconciliation, the golden cases, the TTL dry run. It uses
   no provider key, so it cannot spend money and cannot fail for reasons outside the repository. To
   run it by hand: **Actions → nightly → Run workflow**, and tick `dry_run` (the default).

**Cost at expected volume (one nightly run: 15 Tier-S sources, ≤60 judged candidates, ≤5 briefs).**

| Provider | What it costs here | Free-tier limit | What would trigger a paid tier | Guard already in the code |
|---|---|---|---|---|
| Gemini (LLM) | **$0** — ~60 calls/night, ~4k tokens | free RPM/RPD; the free tier is what this system was drilled against | sustained 429s, or consistently >15k output tokens/night | per-gate token caps (planner 60k, judge 400k, writer 600k) refuse the call and report the gate as `capped` instead of spending |
| Groq / Cerebras (failover) | **$0** — used only when Gemini refuses | free tiers | the same volume, on the fallback path | the chain stops at the first provider that answers; a 402 or 404 fails over instead of retrying |
| Ollama (local) | **$0** | none — your own machine | never (it is local) | n/a |
| eBay Browse (Tier-A) | **$0** — ~20 calls/night | 5,000 calls/day | >5,000 calls/day (≈250× this volume) | `budget_per_day: 500` per source in `sources.yaml`, enforced by a token bucket and the spend ledger, with an alert at 80% |
| GitHub (Tier-A) | **$0** | 5,000 req/hour (PAT) | >120,000 calls/day | same budget mechanism |
| Best Buy / Serper / Product Hunt / Walmart / SearchAPI | **$0** — 0 calls until you enable them | free tiers | enabling them at all | they ship `enabled: false`; enabling a source is an approval-class change (AGENT.md §3) |
| Postgres (WSL, local) | **$0** | n/a — local | never | n/a |
| Machine time | **$0** beyond your electricity | n/a | n/a | the nightly is single-threaded and rate-limited to the sources' declared rps |

**The one cap that matters:** the LLM gates are capped by tokens *and* the sources by calls per day,
and both caps refuse the work rather than exceeding it. When a cap is hit, the run says so
(`capped`) and leaves the remaining candidates unjudged — they show up as `partial` in the eval report
rather than as a quietly smaller number.

## 6. Troubleshooting — the five most likely failures

| # | Symptom | What is happening | Fix |
|---|---|---|---|
| 1 | **A provider key is rejected, or its quota is zero on day one.** `401 invalid api key`, `429`, or `402 payment required` in the LLM drill; a source reports `skipped` | The key is missing, mistyped, revoked, or the account's free tier is already spent. Cerebras in particular answers `402 payment required` on a valid key. (A Gemini key can look fine and still 429: the free tier's daily budget is small and the drill spends it.) | Leave the key blank or fix it — the source/gate skips **with a reason**, nothing else fails. Re-run `uv run python -m scripts.llm_drill` to see which provider actually answered. Free-tier exhaustion is time-based: the daily quota returns, or the chain moves to the next provider |
| 2 | **Wrong region (marketplace scope).** An eBay token works but Browse returns `errorId 2001`/no items; a Walmart or Product Hunt call returns a redirect instead of JSON | These APIs are region- and marketplace-scoped: a production keyset is bound to a marketplace (EBAY_US vs EBAY_DE), and a token minted for one host is rejected by another. The Tier-A plugins also refuse a host that is not in the registry's egress allowlist, which is what a redirected call looks like from inside the system | Check the marketplace/region on the keyset in the developer console, then set the matching base URL in `config/sources.yaml` for that source (the allowlist is derived from the registry, so the host must be listed there too). The refusal message names the host it refused |
| 3 | **OAuth expiry.** eBay calls start failing mid-run with `invalid_access_token`/`401`; Product Hunt and Walmart behave the same | OAuth access tokens live ~2 hours; refresh tokens are longer-lived but revocable. This build fetches a fresh token per run and does not persist one (documented in `sources/tier_a/ebay.py`) — so a failure here means the *refresh* material changed, not the clock | Re-mint the credentials in the developer console (rotate the cert/secret), paste the new value into `config/secrets.local.yaml`, and re-run one source: `uv run python -m trend_analyst.pipeline.orchestrator --layers L2 --source ebay --json`. A revoked grant needs the console, not a retry |
| 4 | **Quota zero-day / the run stops early with `capped`.** The run finishes but a gate reports `capped`, or candidates stay unjudged | The per-gate token cap or a source's daily budget was reached. This is the leash working, not a bug: `capped` is deliberately a different status from `degraded`, because nothing failed | Read what was skipped (`--json`, `notes`), let the day roll over (budgets are per calendar day), or raise a budget deliberately — which is an approval-class change (AGENT.md §3). Never delete ledger rows to clear it |
| 5 | **Postgres is unreachable.** `connection refused` on 5432; or a connection/migration *hangs for ~2 minutes* and then works | Two different causes: (a) the WSL cluster is not running (WSL2 shuts an idle distro down); (b) the DSN says `localhost`, which Windows resolves to IPv6 `::1` first — WSL black-holes it, so the client waits out the TCP timeout | (a) `bash scripts/db_up.sh` (also parks the keep-alive so it stays up). (b) Make the DSN use `127.0.0.1`: `grep url config/secrets.local.yaml`. `password authentication failed` instead means the DB and the file disagree — re-run `scripts/provision_pg.sh`, which re-syncs both |

Two more that are common but not in the five: `uv sync` trying to download a Python build (export
`UV_PYTHON_DOWNLOADS=never UV_NO_MANAGED_PYTHON=1` first), and a missing
`config/secrets.local.yaml` (copy the template; the provisioning script fills in the DSN).

---

## 7. What the human should check, and sign

The brief's definition of done requires a person to run this file alone and reach a green health
check. Working through §4 and stopping at `exit=0` is that test. When it passes, record it in
`docs/evidence/P5-signoff.md` (one line: date, what was run, what the health output said) — that file
is the signature the brief asks for, and it is only meaningful if a human wrote it.
