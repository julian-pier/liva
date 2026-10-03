#!/usr/bin/env bash
set -euo pipefail

ROOT="/opt/liva"
LOCK_DIR="$ROOT/var/locks"
LOCK_FILE="$LOCK_DIR/schoolsync_gcal_pipeline.lock"
FETCH_PROBE_LOG="$ROOT/var/schoolsync/schoolsync_fetch_probe.log"
PYTHON_BIN="$ROOT/venv/bin/python"
export HOME="/var/lib/liva"
export PLAYWRIGHT_BROWSERS_PATH="/var/lib/liva/.cache/ms-playwright"
export XDG_CACHE_HOME="/var/lib/liva/.cache"

mkdir -p "$ROOT/logs"
mkdir -p "$LOCK_DIR"
mkdir -p "$ROOT/var/schoolsync"
cd "$ROOT"

log() {
  printf '%s | %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

run_retry() {
  local label="$1"
  local cmd="$2"
  local attempts="${3:-2}"
  local delay_sec="${4:-45}"
  local i=1

  while [ "$i" -le "$attempts" ]; do
    log "START ${label} (attempt ${i}/${attempts})"
    if eval "$cmd"; then
      log "OK ${label}"
      return 0
    fi
    log "FAIL ${label} (attempt ${i}/${attempts})"
    if [ "$i" -lt "$attempts" ]; then
      sleep "$delay_sec"
    fi
    i=$((i + 1))
  done
  return 1
}

# Hold lock for whole pipeline run
exec 9>"$LOCK_FILE"
if ! /usr/bin/flock -n 9; then
  log "SKIP: pipeline already running (lock: $LOCK_FILE)"
  exit 0
fi

log "START schoolsync_fetch"
if "$PYTHON_BIN" scripts/webuntis_fetch_today.py >"$FETCH_PROBE_LOG" 2>&1; then
  log "OK schoolsync_fetch"
else
  if grep -q "Login state invalid" "$FETCH_PROBE_LOG"; then
    log "INFO auth expired -> running headless auth bootstrap"
    run_retry "schoolsync_auth_bootstrap" "\"$PYTHON_BIN\" scripts/webuntis_auth_bootstrap.py --headless --max-wait-seconds 180" 2 20
    run_retry "schoolsync_fetch_after_auth" "\"$PYTHON_BIN\" scripts/webuntis_fetch_today.py" 2 45
  else
    cat "$FETCH_PROBE_LOG"
    exit 1
  fi
fi

run_retry "gcal_sync_school" "\"$PYTHON_BIN\" scripts/google_calendar_sync_schoolsync.py" 2 45

log "DONE schoolsync_gcal_pipeline"
