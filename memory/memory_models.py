from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LogicalFileSpec:
    logical_name: str
    title: str
    folder_name: str
    filename: str
    sections: tuple[str, ...]
    section_labels: dict[str, str]
    description: str
    default_strategy: str
    domain_tags: tuple[str, ...] = ()
    read_priority: int = 50


ROOT_DIRNAME_DEFAULT = "memory"
ARCHIVE_FOLDER_NAME = "90_archive"
DEFAULT_CONTEXT_MAX_ITEMS = 8

FOLDER_ORDER: tuple[str, ...] = (
    "00_profile",
    "10_athlete",
    "20_health",
    "30_projects",
    "40_reviews",
    ARCHIVE_FOLDER_NAME,
)

LOGICAL_FILE_SPECS: dict[str, LogicalFileSpec] = {
    "master_profile": LogicalFileSpec(
        logical_name="master_profile",
        title="Master-Profil",
        folder_name="00_profile",
        filename="master_profile.md",
        sections=("IDENTITAET", "LANGFRISTIGE_ZIELE", "PRAEFERENZEN", "RAHMENBEDINGUNGEN"),
        section_labels={
            "IDENTITAET": "Identität",
            "LANGFRISTIGE_ZIELE": "Langfristige Ziele",
            "PRAEFERENZEN": "Präferenzen",
            "RAHMENBEDINGUNGEN": "Rahmenbedingungen",
        },
        description="Stabile Identitätsmerkmale, langfristige Ziele, Präferenzen und feste Leitplanken.",
        default_strategy="replace_section",
        domain_tags=("profile", "health"),
        read_priority=95,
    ),
    "athlete_dossier": LogicalFileSpec(
        logical_name="athlete_dossier",
        title="Athleten-Dossier",
        folder_name="10_athlete",
        filename="athlete_dossier.md",
        sections=("AKTUELLE_PHASE", "STAERKEN", "SCHWAECHEN", "AUFFAELLIGE_MUSTER", "OFFENE_PUNKTE"),
        section_labels={
            "AKTUELLE_PHASE": "Aktuelle Phase",
            "STAERKEN": "Stärken",
            "SCHWAECHEN": "Schwächen",
            "AUFFAELLIGE_MUSTER": "Auffällige Muster",
            "OFFENE_PUNKTE": "Offene Punkte",
        },
        description="Lesbares Langform-Dossier zur aktuellen Phase, zu Stärken, Schwächen und stabilen Auffälligkeiten.",
        default_strategy="replace_section",
        domain_tags=("athlete", "training", "health"),
        read_priority=90,
    ),
    "training_history": LogicalFileSpec(
        logical_name="training_history",
        title="Trainingshistorie",
        folder_name="10_athlete",
        filename="training_history.md",
        sections=("AKTUELLER_BLOCK", "RELEVANTE_UMSTELLUNGEN", "BELASTUNGSWECHSEL", "PROBLEME_DURCHBRUECHE"),
        section_labels={
            "AKTUELLER_BLOCK": "Aktueller Block",
            "RELEVANTE_UMSTELLUNGEN": "Relevante Umstellungen",
            "BELASTUNGSWECHSEL": "Belastungswechsel",
            "PROBLEME_DURCHBRUECHE": "Probleme / Durchbrüche",
        },
        description="Verdichtete Historie relevanter Trainingsphasen, Belastungswechsel und bemerkenswerter Umstellungen.",
        default_strategy="append_section_note",
        domain_tags=("athlete", "training", "reviews"),
        read_priority=88,
    ),
    "core_patterns": LogicalFileSpec(
        logical_name="core_patterns",
        title="Kernmuster",
        folder_name="10_athlete",
        filename="core_patterns.md",
        sections=("TRAINING", "VERHALTEN", "ENTSCHEIDUNGEN", "ORGANISATION", "WIEDERKEHRENDE_TENDENZEN"),
        section_labels={
            "TRAINING": "Training",
            "VERHALTEN": "Verhalten",
            "ENTSCHEIDUNGEN": "Entscheidungen",
            "ORGANISATION": "Organisation",
            "WIEDERKEHRENDE_TENDENZEN": "Wiederkehrende Tendenzen",
        },
        description="Verdichtete wiederkehrende Muster über Training, Verhalten, Entscheidungen und Organisation.",
        default_strategy="replace_section",
        domain_tags=("profile", "athlete", "training", "health"),
        read_priority=85,
    ),
    "health_notes": LogicalFileSpec(
        logical_name="health_notes",
        title="Health Notes",
        folder_name="20_health",
        filename="health_notes.md",
        sections=("EINSCHRAENKUNGEN", "VERLETZUNGEN", "RECOVERY", "MEDIZINISCHE_HINWEISE"),
        section_labels={
            "EINSCHRAENKUNGEN": "Einschränkungen",
            "VERLETZUNGEN": "Verletzungen",
            "RECOVERY": "Recovery",
            "MEDIZINISCHE_HINWEISE": "Medizinische Hinweise",
        },
        description="Dauerhafte oder länger relevante Gesundheitsnotizen, Einschränkungen und Recovery-Hinweise.",
        default_strategy="append_section_note",
        domain_tags=("health", "athlete"),
        read_priority=87,
    ),
    "project_memory": LogicalFileSpec(
        logical_name="project_memory",
        title="Projekt-Memory",
        folder_name="30_projects",
        filename="project_memory.md",
        sections=("LIVA", "KOHLEHUB", "ARCHITEKTUR", "DESIGNREGELN", "SYSTEMPRINZIPIEN"),
        section_labels={
            "LIVA": "LIVA",
            "KOHLEHUB": "KohleHub",
            "ARCHITEKTUR": "Architektur",
            "DESIGNREGELN": "Designregeln",
            "SYSTEMPRINZIPIEN": "Systemprinzipien",
        },
        description="Wichtige Projektentscheidungen, Architekturprinzipien und dauerhafte Produktregeln.",
        default_strategy="replace_section",
        domain_tags=("projects", "reviews"),
        read_priority=84,
    ),
    "review_log": LogicalFileSpec(
        logical_name="review_log",
        title="Review-Log",
        folder_name="40_reviews",
        filename="review_log.md",
        sections=("LOG_EINTRAEGE",),
        section_labels={"LOG_EINTRAEGE": "Verdichtete Einträge"},
        description="Chronologische, verdichtete Reviews mit Anlass, Datum und Kernerkenntnis.",
        default_strategy="append_log_entry",
        domain_tags=("reviews", "athlete", "training", "projects", "health"),
        read_priority=80,
    ),
}

DOMAIN_FILE_MAP: dict[str, tuple[str, ...]] = {
    "profile": ("master_profile", "core_patterns"),
    "athlete": ("athlete_dossier", "training_history", "core_patterns", "health_notes", "review_log"),
    "training": ("athlete_dossier", "training_history", "core_patterns", "review_log"),
    "health": ("health_notes", "master_profile", "athlete_dossier", "review_log"),
    "projects": ("project_memory", "review_log"),
    "reviews": ("review_log", "training_history", "project_memory", "athlete_dossier"),
}

SECTION_HINTS: dict[str, dict[str, str]] = {
    "master_profile": {
        "identity": "IDENTITAET",
        "goal": "LANGFRISTIGE_ZIELE",
        "preference": "PRAEFERENZEN",
        "constraint": "RAHMENBEDINGUNGEN",
    },
    "athlete_dossier": {
        "phase": "AKTUELLE_PHASE",
        "strength": "STAERKEN",
        "weakness": "SCHWAECHEN",
        "pattern": "AUFFAELLIGE_MUSTER",
        "note": "OFFENE_PUNKTE",
        "injury": "AKTUELLE_PHASE",
    },
    "training_history": {
        "block": "AKTUELLER_BLOCK",
        "phase": "AKTUELLER_BLOCK",
        "breakthrough": "PROBLEME_DURCHBRUECHE",
        "problem": "PROBLEME_DURCHBRUECHE",
        "training": "RELEVANTE_UMSTELLUNGEN",
        "load": "BELASTUNGSWECHSEL",
    },
    "core_patterns": {
        "training": "TRAINING",
        "behavior": "VERHALTEN",
        "decision": "ENTSCHEIDUNGEN",
        "organization": "ORGANISATION",
        "pattern": "WIEDERKEHRENDE_TENDENZEN",
    },
    "health_notes": {
        "injury": "VERLETZUNGEN",
        "constraint": "EINSCHRAENKUNGEN",
        "recovery": "RECOVERY",
        "medical": "MEDIZINISCHE_HINWEISE",
    },
    "project_memory": {
        "liva": "LIVA",
        "kohlehub": "KOHLEHUB",
        "architecture": "ARCHITEKTUR",
        "design": "DESIGNREGELN",
        "principle": "SYSTEMPRINZIPIEN",
    },
    "review_log": {
        "review": "LOG_EINTRAEGE",
    },
}

SECTION_ALIAS_MAP: dict[str, dict[str, str]] = {
    "master_profile": {
        "identity": "IDENTITAET",
        "identitaet": "IDENTITAET",
        "preferences": "PRAEFERENZEN",
        "preference": "PRAEFERENZEN",
        "constraints": "RAHMENBEDINGUNGEN",
        "constraint": "RAHMENBEDINGUNGEN",
        "goals": "LANGFRISTIGE_ZIELE",
    },
    "athlete_dossier": {
        "current_phase": "AKTUELLE_PHASE",
        "phase": "AKTUELLE_PHASE",
        "strengths": "STAERKEN",
        "weaknesses": "SCHWAECHEN",
        "patterns": "AUFFAELLIGE_MUSTER",
        "auffaellige_muster": "AUFFAELLIGE_MUSTER",
        "notes": "OFFENE_PUNKTE",
        "open_points": "OFFENE_PUNKTE",
        "open_questions": "OFFENE_PUNKTE",
        "preferences": "OFFENE_PUNKTE",
    },
    "training_history": {
        "current_block": "AKTUELLER_BLOCK",
        "phase": "AKTUELLER_BLOCK",
        "changes": "RELEVANTE_UMSTELLUNGEN",
        "relevant_changes": "RELEVANTE_UMSTELLUNGEN",
        "load_changes": "BELASTUNGSWECHSEL",
        "problems": "PROBLEME_DURCHBRUECHE",
        "breakthroughs": "PROBLEME_DURCHBRUECHE",
    },
    "core_patterns": {
        "behavior": "VERHALTEN",
        "behaviour": "VERHALTEN",
        "decisions": "ENTSCHEIDUNGEN",
        "organization": "ORGANISATION",
        "recurring_tendencies": "WIEDERKEHRENDE_TENDENZEN",
        "patterns": "WIEDERKEHRENDE_TENDENZEN",
    },
    "health_notes": {
        "constraints": "EINSCHRAENKUNGEN",
        "injuries": "VERLETZUNGEN",
        "medical": "MEDIZINISCHE_HINWEISE",
        "medical_notes": "MEDIZINISCHE_HINWEISE",
    },
    "project_memory": {
        "liva_notes": "LIVA",
        "active_experiments": "LIVA",
        "experiments": "LIVA",
        "tests": "LIVA",
        "testing": "LIVA",
        "notes": "LIVA",
        "architecture": "ARCHITEKTUR",
        "design_rules": "DESIGNREGELN",
        "system_principles": "SYSTEMPRINZIPIEN",
    },
    "review_log": {
        "entries": "LOG_EINTRAEGE",
        "log_entries": "LOG_EINTRAEGE",
        "reviews": "LOG_EINTRAEGE",
    },
}


def normalize_domain(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in DOMAIN_FILE_MAP else ""


def resolve_logical_files_for_domains(domains: list[str] | tuple[str, ...] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in domains or []:
        domain = normalize_domain(raw)
        if not domain:
            continue
        for logical_name in DOMAIN_FILE_MAP.get(domain, ()):
            if logical_name in seen:
                continue
            seen.add(logical_name)
            ordered.append(logical_name)
    if not ordered:
        return list(LOGICAL_FILE_SPECS.keys())
    return ordered


def normalize_section_token(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.strip().lower().replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]+", "", text)


def resolve_section_name(logical_name: str, section_name: str | None) -> str:
    spec = LOGICAL_FILE_SPECS[logical_name]
    raw = str(section_name or "").strip()
    if not raw:
        return spec.sections[0]

    upper = raw.upper()
    if upper in spec.sections:
        return upper

    normalized = normalize_section_token(raw)
    label_map = {
        normalize_section_token(label): section
        for section, label in spec.section_labels.items()
    }
    if normalized in label_map:
        return label_map[normalized]

    alias_map = SECTION_ALIAS_MAP.get(logical_name) or {}
    if normalized in alias_map:
        return alias_map[normalized]

    hint_map = SECTION_HINTS.get(logical_name) or {}
    if normalized in hint_map:
        return hint_map[normalized]

    if normalized in {"notes", "note", "misc", "general"}:
        return spec.sections[-1]

    return upper


def build_document_template(spec: LogicalFileSpec) -> str:
    lines = [
        f"# {spec.title}",
        "",
        spec.description,
        "",
    ]
    for section in spec.sections:
        label = spec.section_labels.get(section, section.replace("_", " ").title())
        lines.extend(
            [
                f"## {label}",
                f"<!-- SECTION:{section} -->",
                "_Noch offen._",
                f"<!-- /SECTION:{section} -->",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def get_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
