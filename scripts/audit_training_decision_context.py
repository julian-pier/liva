#!/usr/bin/env python3
import argparse
import json
import os
import sys
from urllib.request import Request, urlopen


def load_payload(args):
    if args.json:
      with open(args.json, "r", encoding="utf-8") as fh:
        return json.load(fh)
    if not args.base_url:
      raise SystemExit("--base-url or --json is required")
    token = os.environ.get(args.token_env) if args.token_env else None
    url = f"{args.base_url.rstrip('/')}/api/v2/actions/training/today/decision-context?date={args.date}"
    headers = {"Accept": "application/json"}
    if token:
      headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req) as resp:
      return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--token-env", default="TOKEN")
    parser.add_argument("--json")
    parser.add_argument("--focus")
    parser.add_argument("--raw-sql-check", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args)
    failures = []
    warnings = []
    body = payload.get("ok") and payload or payload
    planned = body.get("planned_session", {})
    planned_type = ((body.get("session_type_context") or {}).get("planned_session_type"))
    history = body.get("recent_training_history") or {}
    slot_map = body.get("set_slot_reference_map") or {}
    trends = body.get("exercise_trends") or {}
    recovery = body.get("recovery_context") or {}
    calendar = body.get("calendar_context") or {}
    weight = body.get("bodyweight_context") or {}
    constraints = body.get("constraints") or {}

    if recovery.get("available") and recovery.get("actual_sleep_minutes") is not None and recovery.get("sleep_hours") is None:
        failures.append("recovery_context.sleep_hours fehlt trotz actual_sleep_minutes")
    if recovery.get("available") and recovery.get("rmssd") is None:
        failures.append("recovery_context.rmssd fehlt trotz Recovery-Daten")
    if recovery.get("available") and recovery.get("source") and recovery.get("status") in (None, ""):
        failures.append("recovery_context.status fehlt trotz Recovery-Quelle")

    if calendar.get("available"):
        if calendar.get("day_pressure") in (None, ""):
            failures.append("calendar_context.day_pressure fehlt")
        school = calendar.get("school") or {}
        if school.get("available") and school.get("total_minutes") in (None, 0):
            failures.append("calendar_context.school.total_minutes fehlt")
        if not any(item.get("role") == "school_summary" for item in (calendar.get("relevant_events") or [])) and school.get("available"):
            failures.append("calendar_context.relevant_events ohne school_summary")
        training_event = calendar.get("training_event") or {}
        if training_event.get("available") and training_event.get("duration_minutes") is None:
            failures.append("calendar_context.training_event.duration_minutes fehlt")

    if weight.get("available") and weight.get("moving_average_7d") is None:
        failures.append("bodyweight_context.moving_average_7d fehlt")

    if constraints.get("decision_authority") != "gpt_only":
        failures.append("constraints.decision_authority != gpt_only")
    if constraints.get("core_may_decide_progression") is not False:
        failures.append("constraints.core_may_decide_progression != false")
    if constraints.get("core_may_decide_skip") is not False:
        failures.append("constraints.core_may_decide_skip != false")
    if "CORE geplant" in json.dumps(body, ensure_ascii=False):
        failures.append("Decision-context enthält noch 'CORE geplant'")

    for ex in planned.get("exercises") or []:
        name = ex.get("name")
        if not history.get(name):
            failures.append(f"recent_training_history[{name!r}] fehlt oder ist leer.")
        slots = ((slot_map.get(name) or {}).get("slots") or [])
        expected = int(ex.get("planned_sets") or 0)
        if expected and len(slots) != expected:
            failures.append(f"{name}: slot count {len(slots)} != planned_sets {expected}")
        for slot in slots:
            if slot.get("primary_reference", {}).get("available") and slot["primary_reference"].get("set_slot") is None:
                failures.append(f"{name}: primary_reference ohne set_slot")
            for ref in slot.get("secondary_references") or []:
                if ref.get("set_slot") is None:
                    failures.append(f"{name}: secondary_reference ohne set_slot")
                if int(slot.get("set_slot") or 0) == 2 and int(ref.get("set_slot") or 0) == 1:
                    failures.append(f"{name}: slot 2 referenziert set_slot 1")
                if ref.get("source_relation") not in {"related_session_family_slot_match", "other_session_type_slot_match"}:
                    warnings.append(f"{name}: unerwartete secondary source_relation {ref.get('source_relation')}")
        for row in history.get(name) or []:
            if row.get("source_relation") == "same_session_type" and row.get("session_type") != planned_type:
                failures.append(f"{name}: same_session_type aber session_type={row.get('session_type')} != {planned_type}")
            if row.get("source_relation") == "related_session_family" and row.get("session_type") == planned_type:
                warnings.append(f"{name}: related_session_family wirkt exakt gleich wie planned_session_type")
        trend = trends.get(name) or {}
        if trend.get("last_top_set") and trend.get("last_backoff") and trend.get("reference_warning"):
            warnings.append(f"{name}: {trend.get('reference_warning')}")

    quality = body.get("context_quality") or {}
    if "slots_without_reference" in json.dumps(body):
        failures.append("old slots_without_reference existiert noch")
    verdict = "OK" if not failures else "FAIL"
    print("FINAL VERDICT:", verdict)
    if quality:
        print("CONTEXT QUALITY:", json.dumps(quality, ensure_ascii=False))
    if args.focus:
        key = args.focus
        print("FOCUS HISTORY:", json.dumps(history.get(key), ensure_ascii=False, indent=2))
        print("FOCUS CONTEXT:", json.dumps((body.get("exercise_session_context") or {}).get(key), ensure_ascii=False, indent=2))
        print("FOCUS SLOT MAP:", json.dumps(slot_map.get(key), ensure_ascii=False, indent=2))
        print("FOCUS TREND:", json.dumps(trends.get(key), ensure_ascii=False, indent=2))
    for item in warnings:
        print("WARN:", item)
    for item in failures:
        print("FAIL:", item)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
