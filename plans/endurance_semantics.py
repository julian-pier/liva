from __future__ import annotations

import hashlib
import json
from typing import Any


CONTRACT_VERSION = "endurance_execution_v1"


def step_tree(flat_steps: list[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    ids = {str(step.get("id")) for step in flat_steps if step.get("id")}
    tree: dict[str | None, list[dict[str, Any]]] = {}
    for step in flat_steps:
        parent = str(step.get("parent_step_id")) if step.get("parent_step_id") else None
        if parent and parent not in ids:
            raise ValueError("workout step references an unknown parent")
        tree.setdefault(parent, []).append(step)
    for siblings in tree.values():
        siblings.sort(key=lambda item: (int(item.get("sort_order") or 0), str(item.get("id") or "")))
    return tree


def expanded_leaf_steps(flat_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand canonical repeats; recovery after the final rep is intentionally omitted."""
    return execution_steps(flat_steps)


def execution_steps(flat_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the exact ordered program consumed by LIVA, Intervals and Garmin."""
    tree = step_tree(flat_steps)
    output: list[dict[str, Any]] = []

    def expand_node(step: dict[str, Any], ancestors: set[str], repeat_context: dict[str, Any] | None = None) -> None:
        step_id = str(step.get("id") or id(step))
        if step_id in ancestors:
            raise ValueError("cyclic workout step structure")
        children = tree.get(step_id, []) if step.get("id") else []
        if step.get("kind") == "repeat" or children:
            reps = int(step.get("reps") or 0)
            if reps < 1 or not children:
                raise ValueError("repeat step requires reps and children")
            for repetition in range(1, reps + 1):
                context = {
                    "repeat_group_id": step.get("id"),
                    "repeat_label": step.get("notes") or "Wiederholungen",
                    "repeat_index": repetition,
                    "repeat_count": reps,
                }
                for child in children:
                    if repetition == reps and child.get("kind") == "recovery":
                        continue
                    expand_node(child, ancestors | {step_id}, context)
            return
        item = dict(step)
        item.update(repeat_context or {})
        item["execution_index"] = len(output) + 1
        output.append(item)

    for root in tree.get(None, []):
        expand_node(root, set())
    return output


def execution_contract(flat_steps: list[dict[str, Any]]) -> dict[str, Any]:
    program = execution_steps(flat_steps)
    semantic = []
    for step in program:
        execution_target = step.get("resolved") or step.get("target") or {}
        internal_target = step.get("target") or {}
        semantic.append({
            "index": step["execution_index"],
            "kind": step.get("kind"),
            "duration_s": step.get("duration_s"),
            "distance_m": step.get("distance_m"),
            "repeat_index": step.get("repeat_index"),
            "repeat_count": step.get("repeat_count"),
            "notes": step.get("notes"),
            "pace_s_per_km": execution_target.get("pace_s_per_km"),
            "pace_min_s_per_km": execution_target.get("pace_min_s_per_km"),
            "pace_max_s_per_km": execution_target.get("pace_max_s_per_km"),
            "hr_bpm": internal_target.get("hr_bpm"),
            "hr_min_pct": internal_target.get("hr_min_pct"),
            "hr_max_pct": internal_target.get("hr_max_pct"),
            "rpe": internal_target.get("rpe"),
            "rpe_min": internal_target.get("rpe_min", internal_target.get("min") if internal_target.get("metric") == "rpe" else None),
            "rpe_max": internal_target.get("rpe_max", internal_target.get("max") if internal_target.get("metric") == "rpe" else None),
        })
    encoded = json.dumps(semantic, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return {"version": CONTRACT_VERSION, "fingerprint": hashlib.sha256(encoded).hexdigest(), "step_count": len(program)}


def effective_pace_s_per_km(step: dict[str, Any]) -> float | None:
    target = step.get("resolved") or step.get("target") or {}
    direct = target.get("pace_s_per_km")
    if direct:
        return float(direct)
    fast = target.get("pace_min_s_per_km")
    slow = target.get("pace_max_s_per_km")
    if fast and slow:
        # Intervals averages speed at both bounds, which corresponds to the
        # harmonic rather than arithmetic mean of pace.
        return 2 / (1 / float(fast) + 1 / float(slow))
    return float(fast or slow) if (fast or slow) else None


def summarize_steps(flat_steps: list[dict[str, Any]]) -> dict[str, float | bool]:
    duration_s = 0.0
    distance_m = 0.0
    duration_complete = True
    distance_complete = True
    for step in execution_steps(flat_steps):
        duration = float(step.get("duration_s") or 0)
        distance = float(step.get("distance_m") or 0)
        pace = effective_pace_s_per_km(step)
        if duration:
            duration_s += duration
            if distance:
                distance_m += distance
            elif pace:
                distance_m += duration / pace * 1000
            else:
                distance_complete = False
        elif distance:
            distance_m += distance
            if pace:
                duration_s += distance * pace / 1000
            else:
                duration_complete = False
        else:
            duration_complete = False
            distance_complete = False
    return {
        "duration_s": duration_s,
        "distance_m": distance_m,
        "duration_complete": duration_complete,
        "distance_complete": distance_complete,
        "complete": duration_complete and distance_complete,
    }
