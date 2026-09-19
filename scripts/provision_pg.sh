#!/usr/bin/env bash
# Trend Analyst — provision the local dev Postgres (run *inside* WSL Ubuntu, as root).
#
#   wsl -d Ubuntu -u root -- bash /mnt/c/Users/Louis/Documents/Projects/TrendAnalyst/scripts/provision_pg.sh
#
# Idempotent: safe to re-run. It
#   1. starts the PG cluster if it is down
#   2. creates/refreshes the `trend_analyst` login role
#   3. creates the `trend_analyst` database (owner: that role)
#   4. creates the pgvector extension
#   5. writes the DSN into config/secrets.local.yaml (untracked, mode 600) WITHOUT
#      clobbering API keys you may have hand-filled there.
#
# No secret is ever printed by this script. It exists so a fresh machine can be
# rebuilt from the repo (spec section 3 layout, brief section 6 USER_SETUP.md).
set -euo pipefail

PROJ="${TREND_ANALYST_PROJECT:-/mnt/c/Users/Louis/Documents/Projects/TrendAnalyst}"
SECRETS="$PROJ/config/secrets.local.yaml"
DB_USER="trend_analyst"
DB_NAME="trend_analyst"
#: The test suite runs against its own database so a test can migrate up and down
#: freely without ever touching dev data (see tests/conftest.py).
DB_TEST_NAME="trend_analyst_test"
#: 127.0.0.1, never `localhost`: on Windows `localhost` resolves to IPv6 ::1 first, and
#: WSL's port relay black-holes ::1 — every connection then burns ~130 s before falling
#: back to IPv4. Measured: 130.09 s as `localhost`, 0.06 s as `127.0.0.1`.
DB_HOST="127.0.0.1"
DB_PORT="5432"
PG_VERSION="18"

log() { printf '[provision_pg] %s\n' "$*"; }

# --- 1. cluster ---------------------------------------------------------------
if ! pg_lsclusters -h | awk '{print $4}' | grep -qx online; then
  log "cluster ${PG_VERSION}/main is down — starting"
  pg_ctlcluster "$PG_VERSION" main start
fi
log "cluster: $(pg_lsclusters -h | awk '{print $1"/"$2, $4}')"

# --- 2. password: reuse the one already in the secrets file -------------------
PW=""
if [ -f "$SECRETS" ]; then
  PW="$(sed -nE "s#^[[:space:]]*url:.*${DB_USER}:([^@]+)@.*#\1#p" "$SECRETS" | head -1)"
fi
if [ -z "$PW" ]; then
  PW="$(openssl rand -hex 16)"
  log "generated a new database password"
else
  log "reusing the database password already in the secrets file"
fi

# --- 3. role ------------------------------------------------------------------
runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$do\$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    ALTER ROLE ${DB_USER} LOGIN PASSWORD '${PW}';
  ELSE
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${PW}';
  END IF;
END
\$do\$;
SQL
log "role ready: ${DB_USER}"

# --- 4. databases -------------------------------------------------------------
for database in "$DB_NAME" "$DB_TEST_NAME"; do
  if runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='${database}'" | grep -q 1; then
    log "database exists: ${database}"
  else
    runuser -u postgres -- createdb -O "${DB_USER}" "${database}"
    log "database created: ${database}"
  fi

done

# --- 5. pgvector --------------------------------------------------------------
# pgvector is not a "trusted" extension, so CREATE EXTENSION needs superuser. Doing it
# here (rather than letting migration 0001 try) is what lets the app role run the
# migrations on a machine where it has no superuser.
for database in "$DB_NAME" "$DB_TEST_NAME"; do
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d "${database}" \
    -c "CREATE EXTENSION IF NOT EXISTS vector"
done
log "pgvector: $(runuser -u postgres -- psql -tAc "SELECT extname||' '||extversion FROM pg_extension WHERE extname='vector'" -d "${DB_NAME}")"

# --- 6. secrets file (untracked) ---------------------------------------------
DSN="postgresql+psycopg://${DB_USER}:${PW}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
mkdir -p "$PROJ/config"
umask 077
if [ -f "$SECRETS" ]; then
  tmp="$(mktemp)"
  url="  url: \"${DSN}\"" awk 'BEGIN{u=ENVIRON["url"]} /^[[:space:]]*url:/{print u; next} {print}' \
    "$SECRETS" >"$tmp"
  cat "$tmp" >"$SECRETS"
  rm -f "$tmp"
  log "secrets file updated in place (API keys preserved): $SECRETS"
else
  cat >"$SECRETS" <<EOF
# Trend Analyst - LOCAL SECRETS. Untracked (see .gitignore). NEVER commit this file.
# Keys live here rather than in the environment. Fill the blanks by hand as each
# provider gets wired up. See config/secrets.example.yaml for the template.
db:
  url: "${DSN}"
llm:
  gemini_api_key: ""
  groq_api_key: ""
  cerebras_api_key: ""
  ollama_base_url: "http://localhost:11434"
tier_a:
  ebay_client_id: ""
  ebay_client_secret: ""
  github_token: ""
  bestbuy_api_key: ""
  serper_api_key: ""
  producthunt_token: ""
  walmart_client_id: ""
  walmart_client_secret: ""
  searchapi_key: ""
EOF
  log "secrets file created: $SECRETS"
fi
chmod 600 "$SECRETS"

# --- 7. verify (no secret in output) -----------------------------------------
log "redacted DSN: $(sed -E "s#(${DB_USER}:)[^@]+#\1<redacted>#" <<<"$DSN")"
log "secrets perms: $(stat -c '%a %U' "$SECRETS")"
log "OK"
