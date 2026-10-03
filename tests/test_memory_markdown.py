from memory.memory_markdown import append_log_entry, append_section_note, mark_stale, parse_sections, replace_section


SAMPLE = """# Athleten-Dossier

## Aktuelle Phase
<!-- SECTION:AKTUELLE_PHASE -->
Alt.
<!-- /SECTION:AKTUELLE_PHASE -->

## Offene Punkte
<!-- SECTION:OFFENE_PUNKTE -->
_Noch offen._
<!-- /SECTION:OFFENE_PUNKTE -->
"""


def test_replace_section_updates_only_target_block():
    out = replace_section(SAMPLE, "AKTUELLE_PHASE", "- Neuer Block aktiv.", heading_label="Aktuelle Phase")
    sections = parse_sections(out)
    assert sections["AKTUELLE_PHASE"]["content"] == "- Neuer Block aktiv."
    assert sections["OFFENE_PUNKTE"]["content"] == "_Noch offen._"


def test_append_section_note_adds_bullet():
    out = append_section_note(SAMPLE, "OFFENE_PUNKTE", "Mehr Schlafqualität beobachten.", heading_label="Offene Punkte")
    sections = parse_sections(out)
    assert "- Mehr Schlafqualität beobachten." in sections["OFFENE_PUNKTE"]["content"]


def test_append_log_entry_keeps_existing_content_and_appends():
    base = """# Review-Log

## Verdichtete Einträge
<!-- SECTION:LOG_EINTRAEGE -->
### 2026-04-05
- Vorheriger Eintrag.
<!-- /SECTION:LOG_EINTRAEGE -->
"""
    out = append_log_entry(base, "LOG_EINTRAEGE", "### 2026-04-06\n- Neuer Eintrag.", heading_label="Verdichtete Einträge")
    sections = parse_sections(out)
    assert "Vorheriger Eintrag." in sections["LOG_EINTRAEGE"]["content"]
    assert "Neuer Eintrag." in sections["LOG_EINTRAEGE"]["content"]


def test_mark_stale_appends_stale_hint():
    out = mark_stale(SAMPLE, "OFFENE_PUNKTE", "Dieser Punkt ist nicht mehr aktuell.", heading_label="Offene Punkte")
    sections = parse_sections(out)
    assert "Veraltet-Hinweis" in sections["OFFENE_PUNKTE"]["content"]
