#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date, datetime, time
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.autopilot_decision import decide_autopilot, format_decision_summary
from tests.helpers.autopilot_scenarios import SCENARIOS


def _infer_sickness_signal(appmod, day_iso: str) -> dict[str, Any]:
    try:
        conn = appmod.get_hrv_db()
        cur = conn.cursor()
        cols = [row["name"] for row in cur.execute("PRAGMA table_info(hrv_measurements);").fetchall()]
        has_sick_bool = "sickness_bool" in cols
        has_sick_text = "sickness" in cols
        if not has_sick_bool and not has_sick_text:
            conn.close()
            return {"sick": False}
        bool_select = "sickness_bool" if has_sick_bool else "0 AS sickness_bool"
        text_select = "sickness" if has_sick_text else "NULL AS sickness"
        where_parts: list[str] = []
        if has_sick_bool:
            where_parts.append("sickness_bool = 1")
        if has_sick_text:
            where_parts.append("LOWER(COALESCE(sickness, '')) NOT IN ('', '0', 'false', 'no')")
        if not where_parts:
            conn.close()
            return {"sick": False}
        where_sql = " OR ".join(where_parts)
        row = cur.execute(
            f"""
            SELECT ts_measurement, date_utc, {bool_select}, {text_select}
            FROM hrv_measurements
            WHERE {where_sql}
            ORDER BY COALESCE(ts_measurement, date_utc) DESC
            LIMIT 1
            """
        ).fetchone()
        conn.close()
        if not row:
            return {"sick": False}
        sick_ts = row["ts_measurement"] or row["date_utc"]
        if not sick_ts:
            # without timestamp we must not keep sickness active
            return {"sick": False}
        ref_now = datetime.combine(date.fromisoformat(day_iso), time(12, 0, 0))
        txt = str(sick_ts)
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            ts_dt = datetime.fromisoformat(txt)
        except Exception:
            return {"sick": False}
        delta_h = (ref_now - ts_dt).total_seconds() / 3600.0
        if delta_h < 0:
            return {"sick": False, "sick_ts": sick_ts}
        return {"sick": True, "sick_ts": sick_ts, "sick_hours_ago": delta_h}
    except Exception:
        return {"sick": False}


def _map_real_data(day_iso: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    import app as appmod

    state = appmod._autopilot_state_snapshot()
    recovery = state.get("recovery") or {}
    sick_signal = _infer_sickness_signal(appmod, day_iso)
    plan_ctx = appmod._autopilot_plan_context_for_day(day_iso)
    slot = {}
    try:
        plan_row = appmod._get_active_plan_row()
        plan = appmod.plan_row_to_dict(plan_row) if plan_row else {}
        week_start = appmod._autopilot_week_start(date.fromisoformat(day_iso))
        week_plan = appmod._autopilot_week_plan(plan, week_start) if plan else []
        slot = next((d for d in week_plan if d.get("day_id") == day_iso), {}) or {}
    except Exception:
        slot = {}

    planned_kind = "train"
    if slot.get("kind") == "run":
        planned_kind = "train"
    elif slot.get("kind") == "plan":
        planned_kind = "train"
    elif slot.get("kind") == "rest":
        planned_kind = "rest"

    plan_ctx = {
        **plan_ctx,
        "planned_session_kind": planned_kind,
        "planned_run_kind": str(slot.get("run_kind") or ""),
        "chosen_session": slot.get("session_name") or slot.get("label"),
    }
    signals_ctx = {
        "sick": bool(sick_signal.get("sick")),
        "sick_ts": sick_signal.get("sick_ts"),
        "sick_hours_ago": sick_signal.get("sick_hours_ago"),
        "rhr_state": "high" if (recovery.get("rhr_ratio") or 0) > 1.06 else "normal",
        "hrv_state": "down" if (recovery.get("hrv_ratio") or 1) < 0.90 else "stable",
    }
    metrics = {}
    try:
        plan_row = appmod._get_active_plan_row()
        plan = appmod.plan_row_to_dict(plan_row) if plan_row else {}
        metrics = appmod._compute_adherence(plan, today=date.fromisoformat(day_iso))
    except Exception:
        metrics = {}

    history_ctx = {
        "last_sessions": [],
        "assume_completed_if_missing": True,
        "manual_override_forced_train": False,
        "session_reorder_only": False,
        "metrics": metrics,
        "planned_runs_last_3_weeks": metrics.get("planned_runs_last_3_weeks"),
        "performed_runs_last_3_weeks": metrics.get("performed_runs_last_3_weeks"),
        "days_since_last_run": metrics.get("days_since_last_run"),
    }
    rules_ctx = {}
    return plan_ctx, signals_ctx, history_ctx, rules_ctx


def _scenario_by_name(name: str):
    return next((s for s in SCENARIOS if s.name == name), None)


def _plan_fit_compact_line(decision) -> str:
    inputs = (decision.explain or {}).get("inputs") or {}
    expected = inputs.get("expected_fatigue")
    observed = inputs.get("observed_fatigue")
    fit = inputs.get("plan_fit")
    if expected is None or observed is None or fit is None:
        return "Plan-Fit: unknown"
    status = "passt" if bool(fit) else "mismatch"
    return f"Plan-Fit: expected={expected} · observed={observed} · {status}"


def _print_table(explain: dict[str, Any]) -> None:
    checks = explain.get("checks") or []
    print("")
    print("Checks")
    print("rule                  | passed | effect                | why")
    print("----------------------+--------+-----------------------+-------------------------------")
    for c in checks:
        rule = str(c.get("rule") or "")[:20].ljust(20)
        passed = ("yes" if c.get("passed") else "no").ljust(6)
        effect = str(c.get("effect") or "")[:21].ljust(21)
        why = str(c.get("why") or "")
        print(f"{rule} | {passed} | {effect} | {why}")


def _print_metrics_and_debt(explain: dict[str, Any]) -> None:
    inputs = explain.get("inputs") or {}
    metrics = inputs.get("metrics") or {}
    if metrics:
        print("")
        print("Metrics")
        keys = [
            "logged_done_7d",
            "assumed_done_7d",
            "skipped_gym_7d",
            "skipped_runs_7d",
            "actual_7d",
            "override_count_7d_whitelisted",
            "override_types_7d",
        ]
        for k in keys:
            if k in metrics:
                print(f"- {k}: {metrics.get(k)}")
    debt = inputs.get("cardio_debt") or {}
    if debt:
        print("")
        print("Cardio Debt")
        for k in ("planned_runs", "performed_runs", "days_since_last_run", "skip_rate", "cardio_debt"):
            if k in debt:
                print(f"- {k}: {debt.get(k)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Explain autopilot decision for one day.")
    parser.add_argument("--day", default=date.today().isoformat(), help="ISO date, e.g. 2026-02-15")
    parser.add_argument("--scenario", default="s1_plan_rest_forces_rest", help="Scenario name for injected mode")
    parser.add_argument("--use-real-data", action="store_true", help="Read real backend state/loaders")
    parser.add_argument("--explain-compact", action="store_true", help="Print only final + one-liner + summary")
    args = parser.parse_args()

    if args.use_real_data:
        plan_ctx, signals_ctx, history_ctx, rules_ctx = _map_real_data(args.day)
    else:
        scn = _scenario_by_name(args.scenario)
        if scn is None:
            names = ", ".join(s.name for s in SCENARIOS)
            raise SystemExit(f"Unknown scenario '{args.scenario}'. Available: {names}")
        plan_ctx = scn.plan_ctx
        signals_ctx = scn.signals_ctx
        history_ctx = scn.history_ctx
        rules_ctx = scn.rules_ctx

    decision = decide_autopilot(
        day=args.day,
        plan_ctx=plan_ctx,
        signals_ctx=signals_ctx,
        history_ctx=history_ctx,
        rules_ctx=rules_ctx,
    )
    explain = decision.explain
    summary = explain.get("summary") or {}
    primary = summary.get("primary")
    contributors = summary.get("contributors") or []
    decision_meta = explain.get("decision") or {}

    print(f"Day: {args.day}")
    print(f"Final: {decision.final_kind}  mode={decision.mode}  chosen_session={decision.chosen_session}")
    print(f"Source: {decision_meta.get('final_kind_source')}  effect={decision_meta.get('applied_effect')}")
    print(f"One-liner: {decision.user_one_liner}")
    print(_plan_fit_compact_line(decision))
    print(format_decision_summary(decision))
    print(f"Primary: {primary}")
    if contributors:
        print("Contributors: " + ", ".join(str(c) for c in contributors))
    if decision.chosen_session == "Run Z2" and str(decision_meta.get("final_kind_source") or "").startswith("rule:CARDIO_REINTRO"):
        print("Reintro triggered because: runs long skipped + recovery good + no gym conflict.")

    if args.explain_compact:
        return 0

    print("")
    print("Inputs")
    for k, v in (explain.get("inputs") or {}).items():
        print(f"- {k}: {v}")
    _print_metrics_and_debt(explain)
    _print_table(explain)
    print("")
    d = explain.get("decision") or {}
    print(f"Decision reason: {d.get('reason')}  confidence={d.get('confidence')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
