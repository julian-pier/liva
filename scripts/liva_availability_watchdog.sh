#!/usr/bin/env bash
set -u

# Fast, dependency-light availability check for the public LIVA and MCP
# endpoints. This runs as root from systemd and only restarts a component after
# two failed probes, so a transient network blip does not cause a restart storm.

APP_URL="https://liva.example.com/"
APP_LOCAL_URL="http://127.0.0.1:5000/"
MCP_URL="https://liva.example.com/healthz"
MCP_LOCAL_URL="http://127.0.0.1:8765/healthz"
STATE_DIR="/run/liva-availability-watchdog"
FAIL_FILE="${STATE_DIR}/failures"

mkdir -p "$STATE_DIR"
failures=0
if [[ -r "$FAIL_FILE" ]]; then
    read -r failures < "$FAIL_FILE" || failures=0
fi
[[ "$failures" =~ ^[0-9]+$ ]] || failures=0

probe() {
    curl --silent --show-error --fail --connect-timeout 3 --max-time 8 \
        --output /dev/null "$1"
}

app_local_ok=0
app_external_ok=0
mcp_local_ok=0
mcp_external_ok=0
probe "$APP_LOCAL_URL" && app_local_ok=1
probe "$APP_URL" && app_external_ok=1
probe "$MCP_LOCAL_URL" && mcp_local_ok=1
probe "$MCP_URL" && mcp_external_ok=1

if (( app_local_ok == 1 && app_external_ok == 1 && mcp_local_ok == 1 && mcp_external_ok == 1 )); then
    printf '0\n' > "$FAIL_FILE"
    exit 0
fi

failures=$((failures + 1))
printf '%s\n' "$failures" > "$FAIL_FILE"

# Require two consecutive failed checks (the timer runs once per minute).
if (( failures < 2 )); then
    logger -t liva-availability-watchdog \
        "probe failed (app_local=${app_local_ok}, app_external=${app_external_ok}, mcp_local=${mcp_local_ok}, mcp_external=${mcp_external_ok}); waiting for confirmation"
    exit 0
fi

if (( app_local_ok == 0 )); then
    logger -t liva-availability-watchdog "local app unavailable; restarting liva.service"
    systemctl restart --no-block liva.service
fi
if (( mcp_local_ok == 0 )); then
    logger -t liva-availability-watchdog "local MCP unavailable; restarting liva-mcp-remote.service"
    systemctl restart --no-block liva-mcp-remote.service
fi
if (( app_external_ok == 0 || mcp_external_ok == 0 )); then
    logger -t liva-availability-watchdog "public endpoint unavailable; reapplying tailscale-funnel.service"
    systemctl restart --no-block tailscale-funnel.service
fi

# Give the restarted component one interval to recover before counting again.
printf '0\n' > "$FAIL_FILE"
