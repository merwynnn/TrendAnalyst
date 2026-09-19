#!/usr/bin/env bash
# Trend Analyst — release the local dev database.
#
#     bash scripts/db_down.sh
#
# Terminates the WSL distribution, which stops Postgres and the keep-alive parked by
# scripts/db_up.sh. WARNING: this stops EVERYTHING running in that distribution, not
# just Postgres — other WSL work in the same distro dies with it.
set -uo pipefail

DISTRO="${TA_WSL_DISTRO:-Ubuntu}"
log() { printf '[db_down] %s\n' "$*"; }

if MSYS_NO_PATHCONV=1 wsl --terminate "$DISTRO" >/dev/null 2>&1; then
  log "terminated $DISTRO (postgres stopped, keep-alive released)"
  log "reversible in one command: bash scripts/db_up.sh"
else
  log "could not terminate $DISTRO (already stopped?)"
fi
