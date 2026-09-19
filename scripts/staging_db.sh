#!/usr/bin/env bash
# Trend Analyst — create or drop the STAGING database used by scripts/runbook_drill.py.
#
#   wsl -d Ubuntu -u root -- bash scripts/staging_db.sh create
#   wsl -d Ubuntu -u root -- bash scripts/staging_db.sh drop
#
# Why this exists as a shell helper rather than in Python: creating a database needs a role
# with CREATEDB, and the application role deliberately does not have it. The app connects as
# `trend_analyst` (login, no CREATEDB, no SUPERUSER); only this helper, run as the postgres
# superuser inside WSL, may create or drop the staging copy. A Python process holding the app
# DSN therefore *cannot* create or destroy a database, which is the point.
#
# The staging database is a throwaway: the drill drops and recreates it on every run, so it
# must never be pointed at anything a human cares about. It is named with a fixed suffix and
# this script refuses to touch a database that does not end in `_staging`.
set -euo pipefail

ACTION="${1:-}"
DB_USER="trend_analyst"
DB_STAGING="trend_analyst_staging"

log() { printf '[staging_db] %s\n' "$*"; }

case "$ACTION" in
  create|drop) ;;
  *) echo "usage: $0 create|drop" >&2; exit 2 ;;
esac

# A guard, not a formality: DROP DATABASE is irreversible and this script runs as a superuser.
case "$DB_STAGING" in
  *_staging) ;;
  *) echo "refusing: '$DB_STAGING' does not end in _staging" >&2; exit 2 ;;
esac

# The cluster must be up. WSL kills idle distros, so scripts/db_up.sh parks a keep-alive.
if ! pg_lsclusters -h | awk '{print $4}' | grep -qx online; then
  log "cluster is down - starting it"
  pg_ctlcluster 18 main start
fi

if [ "$ACTION" = "create" ]; then
  # WITH (FORCE) (PG 13+) terminates leftover connections instead of failing on them: a drill
  # that died halfway leaves its session behind, and refusing to clean it up would make the
  # next run depend on a timeout.
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q \
    -c "DROP DATABASE IF EXISTS ${DB_STAGING} WITH (FORCE)" \
    -c "CREATE DATABASE ${DB_STAGING} OWNER ${DB_USER}"
  # pgvector must be installed by a superuser or migration 0001 fails - the same step
  # provision_pg.sh performs for the dev and test databases.
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q -d "${DB_STAGING}" \
    -c "CREATE EXTENSION IF NOT EXISTS vector"
  log "created ${DB_STAGING} (owner ${DB_USER}, pgvector) - migrations are run by the drill"
else
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q \
    -c "DROP DATABASE IF EXISTS ${DB_STAGING} WITH (FORCE)"
  log "dropped ${DB_STAGING}"
fi
