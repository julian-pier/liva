from __future__ import annotations

from dataclasses import dataclass

from analysis.progression_rules import (
    DEFAULT_CONFIG,
    SetPerformance,
    choose_best_reference_candidate,
    classify_top_backoff_slots,
    compare_set_progress,
    evaluate_plan_target_status,
    normalize_set_performance,
)


@dataclass
class Slot:
    tb_type: str | None
    tb_slot: int | None
    reps: float | None
    weight: float | None
    rpe: float | None


def _slot_to_perf(slot: Slot) -> SetPerformance:
    return SetPerformance(
        weight=slot.weight,
        reps=slot.reps,
        rpe=slot.rpe,
        tb_type=slot.tb_type if slot.tb_type in {"T", "B"} else None,
        tb_slot=slot.tb_slot,
    )


def _perf_to_slot(perf: SetPerformance) -> Slot:
    return Slot(perf.tb_type, perf.tb_slot, perf.reps, perf.weight, perf.rpe)


def classify_tb_slots(sets: list[dict], pct: float = 0.05, abs_threshold: float = 5.0) -> list[Slot]:
    config = DEFAULT_CONFIG
    if pct != config.top_backoff_pct or abs_threshold != config.top_backoff_abs_threshold:
        config = type(DEFAULT_CONFIG)(
            **{
                **DEFAULT_CONFIG.__dict__,
                "top_backoff_pct": float(pct),
                "top_backoff_abs_threshold": float(abs_threshold),
            }
        )
    perf_sets = []
    for idx, raw in enumerate(sets):
        perf_sets.append(
            SetPerformance(
                weight=raw.get("weight"),
                reps=raw.get("reps"),
                rpe=raw.get("rpe"),
                set_number=raw.get("set_number"),
                order_idx=idx,
            )
        )
    return [_perf_to_slot(s) for s in classify_top_backoff_slots(perf_sets, config)]


def find_last_reference(last_slots: list[Slot], tb_type: str | None, tb_slot: int | None) -> Slot | None:
    current = SetPerformance(tb_type=tb_type if tb_type in {"T", "B"} else None, tb_slot=tb_slot)
    candidates = [_slot_to_perf(s) for s in last_slots if s.tb_type == tb_type]
    picked = choose_best_reference_candidate(current, candidates)
    return _perf_to_slot(picked) if picked else None


def evaluate_plan_status(set_data: dict, target: dict | None) -> tuple[str, str]:
    return evaluate_plan_target_status(set_data, target)


def evaluate_simple_status(set_data: dict, last_ref: Slot | None, tb_type: str | None) -> tuple[str, str]:
    identity = {
        "exercise_name": "slot_reference",
        "canonical_exercise": "slot_reference",
        "device": "slot",
        "variation": "default",
        "laterality": "bilateral",
    }
    current = normalize_set_performance({**set_data, **identity, "tb_type": tb_type}) or SetPerformance()
    reference = normalize_set_performance({**(_slot_to_perf(last_ref).__dict__ if last_ref else {}), **identity}) if last_ref else None
    result = compare_set_progress(current, reference)
    if result.status == "progress":
        return "good", result.reason_text
    if result.status == "regress":
        return "bad", result.reason_text
    return "ok", result.reason_text
