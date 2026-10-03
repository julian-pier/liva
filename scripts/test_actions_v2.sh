#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${LIVA_API:-}" ]]; then
  echo "Missing env var: LIVA_API, e.g. export LIVA_API='http://localhost:5000'" >&2
  exit 1
fi

if [[ -z "${AI_API_TOKEN:-}" ]]; then
  echo "Missing env var: AI_API_TOKEN, e.g. export AI_API_TOKEN='...'" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Missing dependency: jq" >&2
  exit 1
fi

BASE="${LIVA_API%/}/api/v2/actions"
AUTH_HEADER="Authorization: Bearer ${AI_API_TOKEN}"
JSON_HEADER="Content-Type: application/json"

# Default run may mutate local LIVA DB state for V2 live commands.
# Set ACTIONS_V2_MEMORY_WRITE=1 to test real Memory markdown writes.
# Set ACTIONS_V2_RUN_CREATE=1 to insert a real run row.
# Set ACTIONS_V2_REMOTE_LIVE=1 to execute real remote adapters.
# Default remote live target is a Tapo/Kasa power action. Govee-only actions
# such as brightness/color require ACTIONS_V2_REMOTE_DEVICE=desk_strip and a
# matching request body in a manual curl.
: "${ACTIONS_V2_MEMORY_WRITE:=0}"
: "${ACTIONS_V2_RUN_CREATE:=0}"
: "${ACTIONS_V2_REMOTE_LIVE:=0}"
: "${ACTIONS_V2_REMOTE_DEVICE:=monitor_links}"
: "${ACTIONS_V2_REMOTE_POWER:=off}"

section() {
  printf '\n\n## %s\n' "$1"
}

get_json() {
  local path="$1"
  curl -sS -H "$AUTH_HEADER" "${BASE}${path}" | jq
}

post_json() {
  local path="$1"
  local body="$2"
  curl -sS -X POST "${BASE}${path}" \
    -H "$AUTH_HEADER" \
    -H "$JSON_HEADER" \
    -d "$body" | jq
}

section "LIVA"
get_json "/liva/today"
get_json "/liva/context"
get_json "/liva/state"

section "CORE"
get_json "/core/state"
post_json "/core/control" '{"command":"set_override","mode":"LIGHT","reason":"actions_v2_test_script"}'

section "Training"
get_json "/training/state"
post_json "/training/data" '{"scope":"sessions","date_from":"2026-01-01","date_to":"2026-04-17","include_exercises":true,"include_sets":true,"limit":10}'
post_json "/training/exercise-data" '{"exercise_names":["Schrägbankdrücken (Smith)","Latzug (eGym)"],"date_from":"2026-01-01","date_to":"2026-04-17","include_history":true,"include_best_sets":true,"include_e1rm":true}'
post_json "/training/adjust" '{"command":"change_sets","day":"Mo","exercise":"Schrägbankdrücken (Smith)","sets":2}'
get_json "/training/plan-state?detail=compact"
get_json "/training/plan-state?detail=full"
post_json "/training/plan-control" '{"command":"set_active_days","days":["Mo","Di","Mi","Do","Fr"]}'

section "Progression"
post_json "/progression/data" '{"mode":"by_exercise","exercise_names":["Schrägbankdrücken (Smith)","Latzug (eGym)"],"metrics":["e1rm","volume_load","top_set_reps","top_set_weight"],"date_from":"2026-01-01","date_to":"2026-04-17","group_by":"session"}'
post_json "/progression/compare" '{"compare_type":"time_ranges","exercise_names":["Schrägbankdrücken (Smith)"],"range_a":{"date_from":"2026-02-01","date_to":"2026-02-28"},"range_b":{"date_from":"2026-03-20","date_to":"2026-04-17"},"metrics":["e1rm","top_set_reps","session_count"]}'

section "Recovery"
get_json "/recovery/state"
post_json "/recovery/data" '{"date_from":"2026-03-01","date_to":"2026-04-17","metrics":["rmssd","heart_rate","sleep_quality","alcohol_flag","sickness_flag"],"group_by":"day"}'
post_json "/recovery/control" '{"command":"annotate_day","date":"2026-04-17","note":"actions_v2_test_script"}'

section "Nutrition"
get_json "/nutrition/state"
post_json "/nutrition/data" '{"mode":"daily_totals","date_from":"2026-03-15","date_to":"2026-04-17","fields":["kcal","protein","carbs","fat","meal_count","logged"],"group_by":"day"}'
post_json "/nutrition/data" '{"mode":"meals","date_from":"2026-04-10","date_to":"2026-04-17","fields":["time","meal_name","foods","kcal","protein"],"limit":20}'
post_json "/nutrition/control" '{"command":"adjust_targets","kcal":2650,"protein":180,"carbs":300,"fat":65}'
get_json "/nutrition/state"

section "Weight"
get_json "/weight/state"
post_json "/weight/data" '{"date_from":"2026-03-01","date_to":"2026-04-17","fields":["weight","moving_avg_7d"],"group_by":"day"}'
post_json "/weight/control" '{"command":"set_checkin_note","date":"2026-04-17","note":"actions_v2_test_script"}'

section "Runs"
get_json "/runs/state"
post_json "/runs/data" '{"date_from":"2026-03-01","date_to":"2026-04-17","fields":["distance_km","pace","avg_hr","run_type","duration_min"],"group_by":"session"}'
if [[ "$ACTIONS_V2_RUN_CREATE" == "1" ]]; then
  post_json "/runs/control" '{"command":"create_run","date":"2026-04-17T18:30:00","distance_km":5.0,"duration_min":25,"avg_hr":145,"run_type":"Easy"}'
else
  echo "Run create skipped. Set ACTIONS_V2_RUN_CREATE=1 to insert a real run."
fi
post_json "/runs/control" '{"command":"set_run_type","date":"2026-04-19","run_type":"Easy"}'
get_json "/runs/state"

section "Memory"
post_json "/liva/read" '{"mode":"memory","include_gpt":true,"limit":10}'
post_json "/liva/act" '{"domain":"memory","command":"store_fact","topic":"project","text":"Actions V2 usememos smoke test."}'

if [[ "$ACTIONS_V2_MEMORY_WRITE" == "1" ]]; then
  section "Memory live mutation"
  post_json "/liva/act" '{"domain":"memory","command":"store_pattern","topic":"training","text":"Actions V2 test-script usememos write.","priority":"low"}'
else
  echo
  echo "## Memory store skipped"
  echo "Set ACTIONS_V2_MEMORY_WRITE=1 to run the live Markdown write."
fi

section "Remote"
if [[ "$ACTIONS_V2_REMOTE_LIVE" == "1" ]]; then
  echo "Remote live enabled: this can switch a real device."
  post_json "/remote/control" "{\"command\":\"device_set_power\",\"device\":\"${ACTIONS_V2_REMOTE_DEVICE}\",\"power\":\"${ACTIONS_V2_REMOTE_POWER}\",\"dry_run\":false}"
else
  post_json "/remote/control" "{\"command\":\"device_set_power\",\"device\":\"${ACTIONS_V2_REMOTE_DEVICE}\",\"power\":\"${ACTIONS_V2_REMOTE_POWER}\",\"dry_run\":true}"
  echo
  echo "Remote live skipped. Set ACTIONS_V2_REMOTE_LIVE=1 to execute the real adapter."
fi
