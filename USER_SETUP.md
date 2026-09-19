# USER_SETUP.md — the human action list

> **Bar:** brief §6. Test of truth: *going from zero to a green health check using only
> this document must work.* Sections marked `TODO(P5)` are finished in P5, once the
> commands they reference exist — this document describes reality, not wishes.

Two of the five sections are already real (Postgres, accounts); the rest land with the
phases that create them.

## 1. Local Postgres (do this first — no account, no cost)

| Step | Command | Expected |
|---|---|---|
| Install/start PG 18 + pgvector inside WSL | `wsl -d Ubuntu -u root -- bash scripts/provision_pg.sh` | `[provision_pg] OK`, cluster `18/main online`, `pgvector: vector 0.8.1` |
| Password + DSN written where? | — | `config/secrets.local.yaml` (untracked, never committed) |
| Stop / start manually | `wsl -d Ubuntu -u root -- pg_ctlcluster 18 main stop\|start` | cluster status line |

The provisioning script creates **two** databases: `trend_analyst` (yours) and
`trend_analyst_test` (the test suite's — so a test can migrate up and down without ever
touching your data).

Nothing is exposed outside this machine: Postgres listens on WSL's localhost, which
Windows reaches at `127.0.0.1:5432`. Use the address literal, not `localhost`: on Windows
`localhost` prefers IPv6 `::1`, which WSL does not forward, and every connection then
waits ~130 s before falling back (README D8).

## 2. Credentials

Keys live in **`config/secrets.local.yaml`** (untracked). Copy the template once:

```bash
cp config/secrets.example.yaml config/secrets.local.yaml   # then paste keys by hand
```

| Credential | Purpose | Where to create it | Free tier | `.yaml` key | Verify command |
|---|---|---|---|---|---|
| Gemini API key | LLM gateway, primary provider (P3) | `aistudio.google.com/apikey` | generous free RPM/RPD | `llm.gemini_api_key` | `TODO(P3)` |
| Groq API key | gateway failover #2 (P3) | `console.groq.com/keys` | free tier | `llm.groq_api_key` | `TODO(P3)` |
| Cerebras API key | gateway failover #3 (P3) | `cloud.cerebras.ai` | free tier | `llm.cerebras_api_key` | `TODO(P3)` |
| Ollama (optional) | gateway last resort, local | `ollama.com/download` | free, local | `llm.ollama_base_url` | `curl` that base URL |
| eBay App ID/Cert | Tier-A: sold-vs-listed (P4) | `developer.ebay.com` → Application Keys | 5,000 calls/day | `tier_a.ebay_*` | `TODO(P4)` |
| GitHub PAT | Tier-A: star/fork velocity (P4) | `github.com/settings/tokens` (fine-grained, public read) | 5,000/hr | `tier_a.github_token` | `TODO(P4)` |
| Best Buy API key | Tier-A: trending electronics (P4) | `developer.bestbuy.com` | instant key, free | `tier_a.bestbuy_api_key` | `TODO(P4)` |
| Serper API key | Tier-A: programmable SERP/PAA (P4) | `serper.dev` | 2,500 free queries | `tier_a.serper_api_key` | `TODO(P4)` |
| Product Hunt token | Tier-A: launch velocity (P4) | `producthunt.com/v2/oauth/applications` | free | `tier_a.producthunt_token` | `TODO(P4)` |
| Walmart keys | Tier-A: mass-market trending (P4) | `developer.walmart.com` | affiliate account | `tier_a.walmart_*` | `TODO(P4)` |
| SearchAPI key | Tier-A: ad-library sampling (P4) | `searchapi.io` | 100 requests | `tier_a.searchapi_key` | `TODO(P4)` |

Tier-S sources need **no key at all** — they run out of the box.

## 3. Accounts and approvals to file on day one

The build never blocks on approvals: every source that needs one ships `enabled: false`
in `config/sources.yaml`, and stays that way until the key exists.

| Action | Expected wait | What to do meanwhile |
|---|---|---|
| eBay developer account → production keyset | hours–days | Tier-A stays disabled; L0 + L1 already produce the ranked table |
| Walmart affiliate approval | days–weeks | ditto |
| SearchAPI account | minutes | ditto |

## 4. First-run checklist

```bash
# 1. database
wsl -d Ubuntu -u root -- bash scripts/provision_pg.sh

# 2. python environment (uses the system interpreter; downloads nothing)
uv sync

# 3. schema
uv run alembic upgrade head
uv run alembic check          # must say: no new upgrade operations detected

# 4. gates
uv run pytest -m "not db"
uv run ruff check .
uv run mypy src config

# 5. health — the acceptance test for setup
uv run python -m trend_analyst.health
echo "exit=$?"
```

**Healthy output looks like** (P0, before any run exists):

```
Trend Analyst health — status: HEALTHY (exit 0)
database: ok (postgres 18.x, pgvector 0.8.1)
runs: none yet (no nightly run has executed)
sources: 15 tier-S enabled, 7 tier-A disabled — registry valid
quota: 0 / 5000 tier-A calls spent
gates: judge keep-rate n/a · eval baseline n/a
```

If any line reads `DOWN` or the exit code is not `0`, jump to §6.

## 5. Scheduler and cost

- **Scheduler:** `TODO(P5)` — the exact GitHub Actions cron workflow is added in P5 (brief §6),
  plus the manual-trigger line.
- **Cost summary:** `TODO(P5)` — the per-provider $0/month table with the cap that would
  trigger each paid tier. Current state: nothing is billed, nothing is scheduled, no
  credential is required for any code that runs today.

## 6. Troubleshooting — the five most likely failures

| # | Symptom | Fix |
|---|---|---|
| 1 | `connection refused` on 5432 | The WSL cluster isn't running (WSL restarts stop it): `wsl -d Ubuntu -u root -- pg_ctlcluster 18 main start` — or just `bash scripts/db_up.sh`, which also parks the keep-alive |
| 2 | `password authentication failed for user "trend_analyst"` | Re-run `scripts/provision_pg.sh` — it re-syncs the password in the DB *and* in `config/secrets.local.yaml` |
| 3 | A connection or `alembic` command **hangs for ~2 minutes** | You are reaching Postgres over IPv6: `localhost` resolves to `::1` first on Windows and WSL black-holes it, so the client waits for the TCP timeout before falling back to IPv4. Use `127.0.0.1` in the DSN (the generated one does), and run migrations with `TA_MIGRATION_TRACE=1` if you need to see where a migration stopped |
| 4 | `config/secrets.local.yaml` missing | `cp config/secrets.example.yaml config/secrets.local.yaml`, then re-run the provisioning script to fill the DSN |
| 5 | A provider key is rejected / quota is zero on day one | Key stays blank in the secrets file; the source is `enabled: false`. Nothing else fails — leave it disabled and file the approval |
| 6 | `uv sync` tries to download a Python build | It must not: use `UV_PYTHON_DOWNLOADS=never UV_NO_MANAGED_PYTHON=1 uv sync` (the venv is built from the system interpreter 3.14.6) |
