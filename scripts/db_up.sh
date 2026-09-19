#!/usr/bin/env bash
# Trend Analyst — bring the local dev database up (idempotent).
#
#     bash scripts/db_up.sh
#
# Why this exists: WSL 2 terminates a distribution shortly after its last session
# exits, and a resident daemon is not enough to hold it — measured on this machine
# with systemd enabled, postgresql enabled+active, and `vmIdleTimeout` set in
# .wslconfig: the port closed within 100s every time. What DOES hold is a detached
# `sleep infinity` parked inside the distro. So this script starts the cluster and
# parks one, which keeps Postgres reachable from Windows at 127.0.0.1:5432.
#
# scripts/db_down.sh releases it (and stops other WSL work in that distro).
#
# Exit 0 when 127.0.0.1:5432 accepts a connection from Windows.
set -uo pipefail

DISTRO="${TA_WSL_DISTRO:-Ubuntu}"
PG_VERSION="${TA_PG_VERSION:-18}"
HOST="${TA_DB_HOST:-127.0.0.1}"
PORT="${TA_DB_PORT:-5432}"
PY_SYS="${TA_SYSTEM_PYTHON:-C:/Users/Louis/AppData/Local/Python/pythoncore-3.14-64/python.exe}"

log() { printf '[db_up] %s\n' "$*"; }

tcp_ok() {
  "$PY_SYS" - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys

s = socket.socket()
s.settimeout(2)
sys.exit(0 if s.connect_ex((sys.argv[1], int(sys.argv[2]))) == 0 else 1)
PY
}

# 1. cluster (booting the distro as a side effect)
if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" -u root -- bash -lc \
  "pg_lsclusters -h | grep -qw online || pg_ctlcluster $PG_VERSION main start" >/dev/null 2>&1; then
  log "cluster ${PG_VERSION}/main online"
else
  log "could not start the cluster — is WSL installed? (wsl -l -v)"
  exit 1
fi

# 2. keep the distro resident across tool calls.
#    `pgrep -x -f` matches the full command line EXACTLY: a plain `pgrep -f
#    'sleep infinity'` would match this very bash invocation (its cmdline contains the
#    pattern), report a false positive and park nothing.
if MSYS_NO_PATHCONV=1 wsl -d "$DISTRO" -u root -- bash -lc \
     "pgrep -x -f 'sleep infinity' >/dev/null" >/dev/null 2>&1; then
  log "keep-alive already parked"
else
  nohup wsl -d "$DISTRO" -u root -- bash -lc "exec sleep infinity" >/dev/null 2>&1 &
  disown
  log "keep-alive parked in $DISTRO (released by scripts/db_down.sh)"
fi

# 3. wait for the port to answer from Windows
for _ in $(seq 1 30); do
  if tcp_ok; then
    log "OK — postgres reachable at ${HOST}:${PORT}"
    exit 0
  fi
  sleep 1
done

log "FAILED — ${HOST}:${PORT} never accepted a connection"
log "check inside WSL: wsl -d $DISTRO -u root -- pg_lsclusters"
exit 1
