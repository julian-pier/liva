from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from memory.memory_models import LOGICAL_FILE_SPECS, SECTION_HINTS, normalize_domain, resolve_section_name


STABLE_HINTS = {
    "always",
    "usually",
    "tends to",
    "pattern",
    "identity",
    "goal",
    "phase",
    "injury",
    "decision",
    "principle",
    "architecture",
}
NOISE_HINTS = {
    "today",
    "this morning",
    "maybe",
    "probably",
    "perhaps",
    "small talk",
    "random",
    "temporary",
}
MAJOR_CHANGE_KINDS = {"phase_change", "injury", "goal_change", "project_decision", "identity"}


def evaluate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_candidate(candidate)
    text = normalized["text"].lower()
    score = 0
    reasons: list[str] = []

    if normalized["stable"]:
        score += 2
        reasons.append("explicit_stable")
    if normalized["evidence_count"] >= 2:
        score += 2
        reasons.append("repeated_observation")
    if normalized["confidence"] >= 0.8:
        score += 1
        reasons.append("high_confidence")
    if normalized["importance"] in {"high", "critical"}:
        score += 1
        reasons.append("high_importance")
    if normalized["kind"] in MAJOR_CHANGE_KINDS:
        score += 2
        reasons.append("major_change_kind")
    if any(token in text for token in STABLE_HINTS):
        score += 1
        reasons.append("stable_semantics")
    if normalized["speculative"] or any(token in text for token in NOISE_HINTS):
        score -= 3
        reasons.append("speculative_or_noise")
    if normalized["confidence"] < 0.45:
        score -= 2
        reasons.append("low_confidence")
    if not normalized["text"]:
        score -= 5
        reasons.append("empty_text")

    accepted = score >= 2
    target_ops = infer_operations(normalized) if accepted else []
    return {
        **normalized,
        "accepted": accepted,
        "score": score,
        "reasons": reasons,
        "operations": target_ops,
    }


def propose_memory_updates(
    *,
    conversation_summary: str = "",
    extracted_facts: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    normalized_candidates = _collect_candidates(extracted_facts or [], candidates or [])
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in normalized_candidates:
        judged = evaluate_candidate(candidate)
        (accepted if judged["accepted"] else rejected).append(judged)

    write_plan = build_write_plan(accepted, conversation_summary=conversation_summary)
    requires_archive = any(bool(item.get("requires_archive")) for item in write_plan)
    return {
        "conversation_summary": conversation_summary.strip(),
        "accepted_candidates": accepted,
        "rejected_candidates": rejected,
        "write_plan": write_plan,
        "requires_archive": requires_archive,
        "dry_run": bool(dry_run),
        "stats": {
            "candidate_count": len(normalized_candidates),
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
        },
    }


def build_write_plan(accepted_candidates: list[dict[str, Any]], *, conversation_summary: str = "") -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in accepted_candidates:
        for op in candidate.get("operations") or []:
            key = (op["logical_file"], op["section"], op["strategy"])
            bucket = grouped.setdefault(
                key,
                {
                    "logical_file": op["logical_file"],
                    "section": op["section"],
                    "strategy": op["strategy"],
                    "candidate_ids": [],
                    "lines": [],
                    "importance": "medium",
                    "confidence": 0.0,
                    "requires_archive": False,
                    "reason": op.get("reason") or "",
                },
            )
            bucket["candidate_ids"].append(candidate["id"])
            bucket["lines"].append(op["line"])
            bucket["confidence"] = max(bucket["confidence"], float(candidate["confidence"]))
            bucket["requires_archive"] = bucket["requires_archive"] or bool(op.get("requires_archive"))
            if candidate["importance"] in {"high", "critical"}:
                bucket["importance"] = candidate["importance"]

    out: list[dict[str, Any]] = []
    for bucket in grouped.values():
        out.append(
            {
                "logical_file": bucket["logical_file"],
                "section": bucket["section"],
                "strategy": bucket["strategy"],
                "content": _render_plan_content(bucket["strategy"], bucket["lines"], conversation_summary),
                "candidate_ids": bucket["candidate_ids"],
                "importance": bucket["importance"],
                "confidence": round(bucket["confidence"], 2),
                "requires_archive": bucket["requires_archive"],
                "reason": bucket["reason"],
            }
        )
    out.sort(key=lambda item: (item["logical_file"], item["section"], item["strategy"]))
    return out


def infer_operations(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    text = candidate["text"].strip()
    if not text:
        return []
    logical_file = candidate["target_file"] or infer_logical_file(candidate)
    spec = LOGICAL_FILE_SPECS[logical_file]
    section = resolve_section_name(logical_file, candidate["section"] or infer_section(logical_file, candidate))
    strategy = candidate["strategy"] or spec.default_strategy
    # Destructive section replacement must always be recoverable, including
    # stable-pattern consolidation (not only explicit phase/goal changes).
    requires_archive = strategy == "replace_section" or (
        strategy == "mark_stale" and candidate["kind"] in MAJOR_CHANGE_KINDS
    )
    line = f"- {text}"

    operations = [
        {
            "logical_file": logical_file,
            "section": section,
            "strategy": strategy,
            "requires_archive": requires_archive,
            "reason": ", ".join(candidate.get("reasons") or []),
            "line": line,
        }
    ]

    if candidate["kind"] in {"phase_change", "injury", "goal_change"} and logical_file != "training_history":
        operations.append(
            {
                "logical_file": "training_history",
                "section": "RELEVANTE_UMSTELLUNGEN" if candidate["kind"] == "goal_change" else "AKTUELLER_BLOCK",
                "strategy": "append_section_note",
                "requires_archive": True,
                "reason": "phase_or_goal_change",
                "line": line,
            }
        )
    if candidate["kind"] == "injury":
        operations.append(
            {
                "logical_file": "health_notes",
                "section": "VERLETZUNGEN",
                "strategy": "append_section_note",
                "requires_archive": True,
                "reason": "injury_or_constraint",
                "line": line,
            }
        )
    if candidate["kind"] in {"pattern", "behavior_pattern"} and logical_file != "core_patterns":
        operations.append(
            {
                "logical_file": "core_patterns",
                "section": "WIEDERKEHRENDE_TENDENZEN",
                "strategy": "replace_section",
                "requires_archive": True,
                "reason": "stable_pattern",
                "line": line,
            }
        )
    if candidate["domain"] == "reviews":
        operations.append(
            {
                "logical_file": "review_log",
                "section": "LOG_EINTRAEGE",
                "strategy": "append_log_entry",
                "requires_archive": False,
                "reason": "review_trace",
                "line": line,
            }
        )
    return _dedupe_operations(operations)


def normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    domain = normalize_domain(candidate.get("domain"))
    kind = str(candidate.get("kind") or "").strip().lower()
    if not kind:
        kind = infer_kind(str(candidate.get("text") or ""), domain)
    target_file = str(candidate.get("target_file") or "").strip()
    if target_file not in LOGICAL_FILE_SPECS:
        target_file = ""
    strategy = str(candidate.get("strategy") or "").strip()
    if strategy not in {"append", "append_log_entry", "append_section_note", "replace_section", "mark_stale"}:
        strategy = ""
    section = str(candidate.get("section") or "").strip().upper()
    confidence = _safe_float(candidate.get("confidence"), 0.65)
    evidence_count = _safe_int(candidate.get("evidence_count"), 1)
    importance = _normalize_importance(candidate.get("importance"))
    stable = _bool(candidate.get("stable")) or evidence_count >= 2
    speculative = _bool(candidate.get("speculative"))
    return {
        "id": str(candidate.get("id") or f"cand_{abs(hash(str(candidate))) % 100000}"),
        "text": str(candidate.get("text") or candidate.get("fact") or "").strip(),
        "domain": domain or infer_domain(str(candidate.get("text") or ""), kind),
        "kind": kind,
        "target_file": target_file,
        "section": section,
        "strategy": strategy,
        "confidence": max(0.0, min(1.0, confidence)),
        "importance": importance,
        "evidence_count": max(0, evidence_count),
        "stable": stable,
        "speculative": speculative,
        "source": str(candidate.get("source") or "").strip(),
        "observed_at": str(candidate.get("observed_at") or _today_iso()).strip(),
    }


def infer_logical_file(candidate: dict[str, Any]) -> str:
    domain = candidate.get("domain") or ""
    kind = candidate.get("kind") or ""
    text = candidate.get("text") or ""
    if kind in {"project_decision", "architecture", "system_rule"} or domain == "projects":
        return "project_memory"
    if kind in {"injury", "health_constraint"} or domain == "health":
        return "health_notes"
    if domain == "reviews" or kind == "review":
        return "review_log"
    if kind in {"identity", "preference"} or domain == "profile":
        return "master_profile"
    if kind in {"pattern", "behavior_pattern"}:
        return "core_patterns"
    if kind in {"phase_change", "goal_change"} or "block" in text.lower():
        return "athlete_dossier"
    if domain in {"athlete", "training"}:
        return "athlete_dossier"
    return "review_log"


def infer_section(logical_file: str, candidate: dict[str, Any]) -> str:
    section_hints = SECTION_HINTS.get(logical_file) or {}
    kind = candidate.get("kind") or ""
    text = (candidate.get("text") or "").lower()
    for token, section in section_hints.items():
        if token in kind or token in text:
            return section
    return LOGICAL_FILE_SPECS[logical_file].sections[0]


def infer_domain(text: str, kind: str) -> str:
    lower = text.lower()
    if "liva" in lower or "kohlehub" in lower or kind in {"project_decision", "architecture", "system_rule"}:
        return "projects"
    if any(token in lower for token in ("injury", "pain", "sleep", "recovery", "fatigue", "constraint")):
        return "health"
    if any(token in lower for token in ("goal", "identity", "preference")):
        return "profile"
    if any(token in lower for token in ("phase", "block", "training", "workout", "load")):
        return "training"
    return "reviews"


def infer_kind(text: str, domain: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ("architecture", "design rule", "system principle", "decision")):
        return "project_decision"
    if any(token in lower for token in ("injury", "pain", "rehab")):
        return "injury"
    if any(token in lower for token in ("phase", "block", "peaking", "deload")):
        return "phase_change"
    if any(token in lower for token in ("goal", "target")):
        return "goal_change"
    if any(token in lower for token in ("pattern", "tends to", "recurring")):
        return "pattern"
    if any(token in lower for token in ("identity", "prefers", "preference")):
        return "identity"
    if domain == "reviews":
        return "review"
    return "note"


def _collect_candidates(extracted_facts: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, start=1):
        item = _coerce_candidate(candidate, fallback_id=f"cand_{idx}")
        if item:
            out.append(item)
    for idx, fact in enumerate(extracted_facts, start=1):
        item = _coerce_candidate(fact, fallback_id=f"fact_{idx}")
        if item:
            out.append(item)
    return out


def _coerce_candidate(value: Any, *, fallback_id: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        item = dict(value)
    elif isinstance(value, str):
        item = {"text": value}
    else:
        return None
    if "text" not in item and "fact" in item:
        item["text"] = item["fact"]
    item.setdefault("id", fallback_id)
    return item


def _render_plan_content(strategy: str, lines: list[str], conversation_summary: str) -> str:
    unique_lines: list[str] = []
    seen: set[str] = set()
    for line in lines:
        clean = line.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        unique_lines.append(clean)
    if strategy == "append_log_entry":
        header = f"### {_today_iso()}"
        if conversation_summary.strip():
            return "\n".join([header, conversation_summary.strip(), *unique_lines]).strip()
        return "\n".join([header, *unique_lines]).strip()
    return "\n".join(unique_lines).strip()


def _dedupe_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in operations:
        key = (item["logical_file"], item["section"], item["strategy"], item["line"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_importance(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high", "critical"}:
        return text
    return "medium"


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()
