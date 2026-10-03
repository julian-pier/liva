from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from liva_memory import MemoryStore, StoreError
from liva_memory.interactive import _ref
from liva_mcp.config import Config
from liva_mcp.memory_v2_service import MemoryV2Service
from liva_mcp.read_service import ReadService
from liva_mcp.tool_registry import TOOL_BY_NAME, call_tool, list_tools


MEMORY_TOOLS = {"liva_memory_context", "liva_memory_source", "liva_memory_pending", "liva_memory_ingest_file", "liva_memory_commit"}


def _service(service: ReadService, vault: Path, runtime: Path) -> ReadService:
    service.config = Config(repo_root=service.config.repo_root, memory_vault=vault, memory_runtime_dir=runtime)
    return service


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault, runtime = tmp_path / "memory-vault", tmp_path / "memory-runtime"
    (vault / "wiki/Ich").mkdir(parents=True)
    (vault / "procedures").mkdir()
    (vault / "wiki/Ich/Profil.md").write_text("# Profil\n\n## Ziel\n\nDie Person möchte langfristig klar planen.\n", encoding="utf-8")
    (vault / "procedures/Memory.md").write_text("# Memory\n\n## Regel\n\nEvidence muss erhalten bleiben.\n", encoding="utf-8")
    store = MemoryStore(vault, runtime)
    store.wiki_reindex()
    store.procedure_reindex()
    return vault, runtime


def _new_commit(key: str) -> dict[str, object]:
    return {"idempotency_key": key, "source": {"title": "Test", "direct_user_context": "Direkter Testkontext.", "assistant_synthesis": "Technische Zusammenfassung.", "temporal_scope": "historical", "topics": ["test"]}, "claims": [{"claim_id": "c1", "text": "Direkter Testkontext.", "evidence_type": "direct_user_statement", "source_section": "direct_user_context"}], "knowledge_operations": [], "compile_result": {"state": "raw_only"}}


def test_registration_and_serializable_schemas() -> None:
    assert MEMORY_TOOLS <= set(TOOL_BY_NAME)
    assert MEMORY_TOOLS <= {entry["name"] for entry in list_tools()}
    for name in MEMORY_TOOLS:
        json.dumps(TOOL_BY_NAME[name].input_schema)


def test_context_handler_calls_core(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    result = call_tool(_service(service, vault, runtime), "liva_memory_context", {"queries": ["langfristig planen"]}, scopes={"liva.read"})
    assert result["ok"] and result["data"]["results"][0]["layer"] == "living_wiki"


def test_procedure_recall_selects_new_nested_skill_without_loading_the_vault(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    store = MemoryStore(vault, runtime)
    store.procedure_reindex()
    skill = vault / "procedures/skills/liva-grill-me/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: liva-grill-me\n---\n\n# LIVA Grill Me\n\nKritischer Ideen-Ablauf.\n", encoding="utf-8")
    result = call_tool(_service(service, vault, runtime), "liva_memory_recall", {"query": "LIVA Grill Me", "scope": "procedures", "depth": "brief"}, scopes={"liva.read"})
    assert result["ok"] and result["data"]["sources_available"] is True
    skill_result = result["data"]["answer_context"][0]
    assert skill_result["path"] == "procedures/skills/liva-grill-me/SKILL.md"
    assert skill_result["text_complete"] is True and skill_result["text_chars"] == len(skill_result["text"])


def test_audit_is_read_only_bounded_and_rejects_unsafe_paths(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    problematic = vault / "wiki/Ich/Audit.md"
    problematic.write_text("---\ntitle: Anderer Titel\ntype: topic\n---\n\n# Audit\n\n[[wiki/Ich/Fehlt|Fehlt]]\n\n[[wiki/Ich/FehltAuch|Fehlt auch]]\n", encoding="utf-8")
    before = problematic.read_text(encoding="utf-8")
    active = _service(service, vault, runtime)
    audit = call_tool(active, "liva_memory_audit", {"scope": "vault", "max_issues": 1}, scopes={"liva.read"})
    assert audit["ok"] and audit["data"]["issues_returned"] == 1
    assert audit["data"]["issues_truncated"] is True and audit["data"]["total_issues"] > 1
    assert problematic.read_text(encoding="utf-8") == before
    selected = call_tool(active, "liva_memory_audit", {"scope": "paths", "paths": ["wiki/Ich/Audit.md"]}, scopes={"liva.read"})
    assert selected["ok"] and selected["data"]["checked_paths"] == ["wiki/Ich/Audit.md"]
    for path in ("../outside.md", "/etc/passwd", "procedures/Memory.md", "wiki/Ich/Unknown.md"):
        rejected = call_tool(active, "liva_memory_audit", {"scope": "paths", "paths": [path]}, scopes={"liva.read"})
        assert rejected["ok"] is False


def test_grill_me_memory_v2_commit_has_source_and_readback(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Die Person arbeitet am besten mit klaren, kleinen nächsten Schritten.", "kind": "personal", "context_queries": ["langfristig planen"]}, scopes={"liva.write"})
    assert begin["ok"]
    candidate = begin["data"]["candidates"][0]
    finish = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"],
        "claims": ["Die Person arbeitet am besten mit klaren, kleinen nächsten Schritten."],
        "route": "living_wiki",
        "changes": [{"action": "replace_section", "target_ref": candidate["section_ref"], "text": "Die Person plant langfristig mit klaren, kleinen nächsten Schritten."}],
        "reason": "bestätigte Grill-Me-Erkenntnis",
    }, scopes={"liva.write"})
    assert finish["ok"] and finish["data"]["reporting"]["may_claim_knowledge_saved"] is True
    assert finish["data"]["verification"]["knowledge_readback"] is True
    recalled = call_tool(active, "liva_memory_recall", {"query": "langfristig planen", "scope": "personal"}, scopes={"liva.read"})
    assert recalled["ok"] and recalled["data"]["sources_available"]


def test_gpt_friendly_begin_finish_is_atomic_and_creates_no_legacy_memo(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Die Person möchte langfristig klar planen.", "kind": "personal"}, scopes={"liva.write"})
    assert begin["ok"] and begin["data"]["source_id"].startswith("src_")
    assert begin["data"]["workflow"]["existing_knowledge_first"] is True
    assert "maintenance_context" in begin["data"] and begin["data"]["candidates"][0]["path"].startswith("wiki/")
    candidate = begin["data"]["candidates"][0]
    finish = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Die Person möchte langfristig klar planen."], "route": "living_wiki", "changes": [{"action": "replace_section", "target_ref": candidate["section_ref"], "text": "Die Person plant langfristig und überprüft Entscheidungen bewusst."}], "reason": "isolierter Fassadentest"}, scopes={"liva.write"})
    assert finish["ok"] and finish["data"]["state"] == "compiled"
    assert finish["data"]["preflight"]["duplicate_guard"] == "passed"
    assert finish["data"]["provenance"]["source_id"] == begin["data"]["source_id"]
    assert "maintenance" in finish["data"]
    assert "überprüft Entscheidungen bewusst" in (vault / "wiki/Ich/Profil.md").read_text(encoding="utf-8")
    assert list((vault / "sources/captures").rglob("*.md"))
    recalled = call_tool(active, "liva_memory_recall", {"query": "langfristig planen", "scope": "personal"}, scopes={"liva.read"})
    assert recalled["ok"] and recalled["data"]["sources_available"]


def test_procedure_finish_updates_existing_canonical_rule_with_readback(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    context = call_tool(active, "liva_memory_context", {"queries": ["Evidence muss erhalten bleiben"], "layers": "procedures"}, scopes={"liva.read"})
    target = next(item["section_ref"] for item in context["data"]["results"] if item["path"] == "procedures/Memory.md")
    begin = call_tool(active, "liva_memory_begin", {"text": "Eine bestehende Procedure muss nach einer Änderung zurückgelesen werden.", "kind": "procedure"}, scopes={"liva.write"})
    finished = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Eine bestehende Procedure muss nach einer Änderung zurückgelesen werden."],
        "route": "procedures", "changes": [{"action": "replace_section", "target_ref": target, "text": "Evidence muss erhalten bleiben. Nach jeder Änderung erfolgt ein Readback."}],
        "reason": "kanonische Procedure gezielt ergänzen",
    }, scopes={"liva.write"})
    assert finished["ok"], finished
    assert finished["data"]["reporting"]["may_claim_knowledge_saved"] is True
    assert finished["data"]["verification"]["knowledge_readback"] is True
    assert "Nach jeder Änderung erfolgt ein Readback." in (vault / "procedures/Memory.md").read_text(encoding="utf-8")
    replay = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Eine bestehende Procedure muss nach einer Änderung zurückgelesen werden."],
        "route": "procedures", "changes": [{"action": "replace_section", "target_ref": target, "text": "Evidence muss erhalten bleiben. Nach jeder Änderung erfolgt ein Readback."}],
        "reason": "kanonische Procedure gezielt ergänzen",
    }, scopes={"liva.write"})
    assert replay["ok"], replay
    assert replay["data"]["replayed"] is True


def test_procedure_finish_uses_digest_bound_ref_when_headings_repeat(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    procedure = vault / "procedures/Doppelte-Ueberschrift.md"
    procedure.write_text("# Doppelte Überschrift\n\n## Regel\n\n## Regel\n\nDie zweite Regel ist kanonisch.\n", encoding="utf-8")
    store = MemoryStore(vault, runtime)
    store.procedure_reindex()
    with store._connect() as db:
        digest = db.execute("SELECT content_sha256 FROM procedure_sections WHERE path=? AND heading=? AND text=?", ("procedures/Doppelte-Ueberschrift.md", "Regel", "Die zweite Regel ist kanonisch.")).fetchone()[0]
    target = _ref("procedures", "procedures/Doppelte-Ueberschrift.md", "Regel", digest)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Die zweite Regel wurde gezielt aktualisiert."}, scopes={"liva.write"})
    finished = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Die zweite Regel wurde gezielt aktualisiert."],
        "route": "procedures", "changes": [{"action": "replace_section", "target_ref": target, "text": "Die zweite Regel wurde gezielt aktualisiert."}],
    }, scopes={"liva.write"})
    assert finished["ok"], finished
    assert "Die zweite Regel wurde gezielt aktualisiert." in procedure.read_text(encoding="utf-8")


def test_gardener_begin_is_retrieval_only_and_finish_creates_atomic_human_notes(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Ein dokumentiertes Ereignis und eine dauerhafte Erkenntnis.", "kind": "historical"}, scopes={"liva.write"})
    assert begin["ok"]
    gardener = begin["data"]["gardener"]
    assert gardener["suggested_note_type"] is None and gardener["split_candidates"] == []
    assert gardener["folder_patterns"]["person_hub"] == "wiki/Menschen/<Person>/<Person>.md"
    result = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Ein dokumentiertes Ereignis und eine dauerhafte Erkenntnis."], "route": "living_wiki",
        "changes": [
            {"action": "create_page", "path": "wiki/Menschen/Test/Test.md", "title": "Test", "note_type": "hub", "text": "## Verlauf\n\nKurze Navigation.", "links": [{"path": "wiki/Menschen/Test/2026-08-28 – Ereignis.md", "label": "Ereignis"}]},
            {"action": "create_page", "path": "wiki/Menschen/Test/2026-08-28 – Ereignis.md", "title": "2026-08-28 – Ereignis", "note_type": "event", "event_date": "2026-08-28", "person": "Test", "text": "Dokumentiertes Ereignis.", "links": [{"path": "wiki/Menschen/Test/Test.md", "label": "Test"}]},
            {"action": "create_page", "path": "wiki/Ich/Falsch/Dauerhafte Erkenntnis.md", "title": "Dauerhafte Erkenntnis", "note_type": "insight", "text": "Wiederverwendbare Erkenntnis.", "links": [{"path": "wiki/Menschen/Test/Test.md", "label": "Test"}]},
        ], "reason": "isolierter Gardener-Test",
    }, scopes={"liva.write"})
    assert result["ok"] and len(result["data"]["changed_paths"]) == 3
    hub = (vault / "wiki/Menschen/Test/Test.md").read_text(encoding="utf-8")
    event = (vault / "wiki/Menschen/Test/2026-08-28 – Ereignis.md").read_text(encoding="utf-8")
    insight = (vault / "wiki/Menschen/Test/Dauerhafte Erkenntnis.md").read_text(encoding="utf-8")
    assert "type: hub" in hub and hub.count("\n# ") == 1 and "cssclasses:\n  - liva-wiki" in hub
    assert "## Quellen" in hub and "|Memory-Quelle]]" in hub
    assert "## Verlauf" in hub and len(hub.split()) < 100  # concise navigation remains GPT-authored
    assert "type: event" in event and "event_date: 2026-08-28" in event and "person: \"Test\"" in event
    assert "type: insight" in insight and "event_date:" not in insight and insight.count("\n# ") == 1
    assert any(item["changed"] and item["canonical_path"] == "wiki/Menschen/Test/Dauerhafte Erkenntnis.md" for item in result["data"]["path_routing"])


def test_gardener_preserves_existing_frontmatter_and_protects_historical_event(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    page = vault / "wiki/Ich/Profil.md"
    page.write_text("---\naliases:\n  - Profil\ncssclasses:\n  - liva-wiki\n---\n\n# Profil\n\n## Ziel\n\nAlter Stand.\n", encoding="utf-8")
    event = vault / "wiki/Ich/Ereignis.md"
    event.write_text("---\ntype: event\nevent_date: 2026-08-01\ncssclasses:\n  - liva-wiki\n---\n\n# Ereignis\n\n## Verlauf\n\nHistorisch.\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    ref = call_tool(active, "liva_memory_context", {"queries": ["Alter Stand"]}, scopes={"liva.read"})["data"]["results"][0]["section_ref"]
    before_frontmatter = page.read_text(encoding="utf-8").split("---\n", 2)[1]
    begin = call_tool(active, "liva_memory_begin", {"text": "Neuer Stand."}, scopes={"liva.write"})
    updated = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Neuer Stand."], "route": "living_wiki", "changes": [{"action": "replace_section", "target_ref": ref, "text": "Neuer Stand."}]}, scopes={"liva.write"})
    assert updated["ok"] and page.read_text(encoding="utf-8").split("---\n", 2)[1] == before_frontmatter
    event_ref = MemoryStore(vault, runtime).memory_context(["Historisch"], "living_wiki")["results"][0]["section_ref"]
    blocked = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Neuer Stand."], "route": "living_wiki", "changes": [{"action": "replace_section", "target_ref": event_ref, "text": "Später."}]}, scopes={"liva.write"})
    assert blocked["ok"] is False and blocked["error"]["code"] == "HISTORICAL_EVENT_IMMUTABLE"


def test_finish_repairs_historical_event_links_and_reports_verified_scope(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    event = vault / "wiki/Ich/Ereignis.md"
    event.write_text("---\ntype: event\nevent_date: 2026-09-03\n---\n\n# Ereignis\n\nHistorischer Text.\n", encoding="utf-8")
    target = vault / "wiki/Ich/Polizei.md"
    target.write_text("# Polizei\n\nBeruflicher Kontext.\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    event_ref = next(item["section_ref"] for item in MemoryStore(vault, runtime).memory_context(["Historischer Text"], "living_wiki")["results"] if item["path"] == "wiki/Ich/Ereignis.md")
    begin = call_tool(active, "liva_memory_begin", {"text": "Das Ereignis soll selbst auf Polizei verweisen.", "kind": "historical"}, scopes={"liva.write"})
    assert begin["ok"] and begin["data"]["storage_status"]["may_claim_knowledge_saved"] is False
    finish = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"],
        "claims": [],
        "route": "living_wiki",
        "changes": [{"action": "ensure_links", "target_ref": event_ref, "links": [{"path": "wiki/Ich/Polizei.md", "label": "Polizei"}]}],
    }, scopes={"liva.write"})
    assert finish["ok"]
    assert finish["data"]["outcome"] == "knowledge_updated"
    assert finish["data"]["verification"]["knowledge_readback"] is True
    assert finish["data"]["reporting"]["may_claim_knowledge_saved"] is True
    assert "Historischer Text." in event.read_text(encoding="utf-8")
    assert "[[wiki/Ich/Polizei|Polizei]]" in event.read_text(encoding="utf-8")


def test_finish_can_append_a_source_linked_historical_correction(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path); active = _service(service, vault, runtime)
    event = vault / "wiki/Ich/Ereignis.md"
    event.write_text("---\ntitle: \"Ereignis\"\ntype: event\nevent_date: 2026-09-03\ncssclasses:\n  - liva-wiki\n---\n\n# Ereignis\n\nHistorischer Text.\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    ref = next(item["section_ref"] for item in MemoryStore(vault, runtime).memory_context(["Historischer Text"], "living_wiki")["results"] if item["path"] == "wiki/Ich/Ereignis.md")
    begin = call_tool(active, "liva_memory_begin", {"text": "Die frühere Formulierung war sachlich falsch.", "kind": "historical"}, scopes={"liva.write"})
    result = call_tool(active, "liva_memory_finish", {"source_id":begin["data"]["source_id"],"claims":["Die frühere Formulierung war sachlich falsch."],"route":"living_wiki","changes":[{"action":"correct_page","target_ref":ref,"text":"Korrekte sachliche Einordnung."}]}, scopes={"liva.write"})
    assert result["ok"] and result["data"]["reporting"]["may_claim_knowledge_saved"] is True
    corrected=event.read_text(encoding="utf-8")
    assert "Historischer Text." in corrected and "## Korrekturen" in corrected and "Memory-Quelle" in corrected


def test_finish_no_change_cannot_be_reported_as_knowledge_storage(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Bereits vorhandener Kontext."}, scopes={"liva.write"})
    finish = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": [], "route": "living_wiki",
        "changes": [{"action": "no_change"}],
    }, scopes={"liva.write"})
    assert finish["ok"]
    assert finish["data"]["state"] == "raw_only"
    assert finish["data"]["outcome"] == "no_change"
    assert finish["data"]["reporting"]["may_claim_knowledge_saved"] is False
    assert finish["data"]["reporting"]["required_summary"] == "No Living Wiki change was made."


def test_gardener_rejects_fake_event_dates_and_unsafe_paths(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Test."}, scopes={"liva.write"})
    bad_date = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Test."], "route": "living_wiki", "changes": [{"action": "create_page", "path": "wiki/Ich/Insight.md", "title": "Insight", "note_type": "insight", "event_date": "2026-08-28", "text": "Test.", "links": [{"path": "wiki/Ich/Profil.md", "label": "Profil"}]}]}, scopes={"liva.write"})
    assert bad_date["ok"] is False
    unsafe = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Test."], "route": "living_wiki", "changes": [{"action": "create_page", "path": "wiki/Ich/../unsafe.md", "title": "Unsafe", "note_type": "topic", "text": "Test.", "links": [{"path": "wiki/Ich/Profil.md", "label": "Profil"}]}]}, scopes={"liva.write"})
    assert unsafe["ok"] is False and unsafe["error"]["code"] == "INVALID_PATH"


def test_gardener_live_shape_begin_refs_are_finish_safe_for_five_replaces_and_three_creates(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    page = vault / "wiki/Menschen/Mathilda.md"
    page.parent.mkdir(exist_ok=True)
    page.write_text("# Mathilda\n\n" + "\n\n".join(f"## Abschnitt {number}\n\nAlter {number}." for number in range(1, 6)) + "\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    begin = call_tool(active, "liva_memory_begin", {"text": "Mathilda-Hub soll gekürzt werden", "kind": "personal", "context_queries": [f"Alter {number}" for number in range(1, 6)]}, scopes={"liva.write"})
    assert begin["ok"]
    refs = {item["text"].splitlines()[0]: item["section_ref"] for item in begin["data"]["candidates"]}
    changes = [{"action": "replace_section", "target_ref": refs[f"Alter {number}."], "text": f"Kurzer Hubtext {number}."} for number in range(1, 6)]
    changes.extend([
        {"action": "create_page", "path": "wiki/Menschen/Mathilda/Mathilda.md", "title": "Mathilda", "note_type": "hub", "text": "## Verlauf\n\nKurze Navigation.", "links": [{"path": "wiki/Menschen/Mathilda/2026-08-08 – Testevent.md", "label": "Testevent"}]},
        {"action": "create_page", "path": "wiki/Menschen/Mathilda/2026-08-08 – Testevent.md", "title": "Testevent", "note_type": "event", "event_date": "2026-08-08", "text": "Dokumentiertes Testereignis.", "links": [{"path": "wiki/Menschen/Mathilda/Mathilda.md", "label": "Mathilda"}]},
        {"action": "create_page", "path": "wiki/Menschen/Mathilda/Testinsight.md", "title": "Testinsight", "note_type": "insight", "text": "Dauerhafte Testeinsicht.", "links": [{"path": "wiki/Menschen/Mathilda/Mathilda.md", "label": "Mathilda"}]},
    ])
    result = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Mathilda-Hub soll kurz sein"], "route": "living_wiki", "changes": changes}, scopes={"liva.write"})
    assert result["ok"] and len(result["data"]["changed_paths"]) == 4
    updated = page.read_text(encoding="utf-8")
    assert all(f"Kurzer Hubtext {number}." in updated for number in range(1, 6))
    event = (vault / "wiki/Menschen/Mathilda/2026-08-08 – Testevent.md").read_text(encoding="utf-8")
    assert event.startswith("---\ntitle: \"Testevent\"\ntype: event\nevent_date: 2026-08-08\nperson: \"Mathilda\"\ncssclasses:\n  - liva-wiki\n---\n\n# Testevent\n")


def test_create_without_parent_links_is_rejected_before_a_markdown_file_exists(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path); active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Nicht unverknüpft anlegen."}, scopes={"liva.write"})
    result = call_tool(active, "liva_memory_finish", {"source_id":begin["data"]["source_id"],"claims":["Nicht unverknüpft anlegen."],"route":"living_wiki","changes":[{"action":"create_page","path":"wiki/Training & Körper/Verwaist.md","title":"Verwaist","note_type":"topic","text":"Ohne Verbindung."}]}, scopes={"liva.write"})
    assert result["ok"] is False and result["error"]["code"] == "MISSING_PARENT_LINKS"
    assert not (vault / "wiki/Training & Körper/Verwaist.md").exists()


def test_project_hub_create_and_parent_link_commit_atomically(service: ReadService, tmp_path: Path) -> None:
    """Regression: the public begin/finish contract accepts a linked project hub."""
    vault, runtime = _vault(tmp_path)
    project = vault / "wiki/Projekte/LIVA – mein System.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        "# LIVA – mein System\n\n## Projektkarte\n\n- [[wiki/Projekte/Bestehend|Bestehend]]\n",
        encoding="utf-8",
    )
    for name in ("Memory V2", "MCP", "Architektur"):
        (vault / f"wiki/Projekte/{name}.md").write_text(f"# {name}\n\nBestehender Kontext.\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    active = _service(service, vault, runtime)
    context = call_tool(active, "liva_memory_context", {"queries": ["Projektkarte"]}, scopes={"liva.read"})
    project_ref = next(item["section_ref"] for item in context["data"]["results"] if item["heading"] == "Projektkarte")
    source_text = "Die Universal Skill Engine ist geplant. Das Konzept ist entschieden; die Umsetzung hat noch nicht begonnen."
    begin = call_tool(active, "liva_memory_begin", {"text": source_text, "kind": "auto"}, scopes={"liva.write"})
    page = "wiki/Projekte/LIVA – mein System/LIVA Universal Skill Engine.md"
    finish = call_tool(
        active,
        "liva_memory_finish",
        {
            "source_id": begin["data"]["source_id"],
            "claims": [source_text],
            "route": "living_wiki",
            "changes": [
                {
                    "action": "create_page",
                    "path": page,
                    "title": "LIVA Universal Skill Engine",
                    "note_type": "hub",
                    "text": "Konzept entschieden, Umsetzung noch nicht begonnen.\n\n## Architektur\n\nMemory V2 bleibt zentral.",
                    "links": [
                        {"path": "wiki/Projekte/LIVA – mein System.md", "label": "LIVA – mein System"},
                        {"path": "wiki/Projekte/Memory V2.md", "label": "Memory V2"},
                        {"path": "wiki/Projekte/MCP.md", "label": "MCP"},
                        {"path": "wiki/Projekte/Architektur.md", "label": "Architektur"},
                    ],
                },
                {
                    "action": "ensure_links",
                    "target_ref": project_ref,
                    "links": [{"path": page, "label": "LIVA Universal Skill Engine"}],
                },
            ],
            "reason": "isolierter realistischer Projekt-Workflow",
        },
        scopes={"liva.write"},
    )
    assert finish["ok"], finish
    assert finish["data"]["reporting"]["may_claim_knowledge_saved"] is True
    assert (vault / page).is_file()
    assert "LIVA Universal Skill Engine" in project.read_text(encoding="utf-8")


def test_begin_source_round_trips_crlf_without_hash_mismatch(service: ReadService, tmp_path: Path) -> None:
    """A pasted Windows-style source must remain readable by finish."""
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    source_text = "Erste Zeile.\r\n\r\nZweite Zeile."
    begin = call_tool(active, "liva_memory_begin", {"text": source_text}, scopes={"liva.write"})
    assert begin["ok"]
    source = call_tool(active, "liva_memory_source", {"source_id": begin["data"]["source_id"]}, scopes={"liva.read"})
    assert source["ok"], source
    assert "Erste Zeile.\n\nZweite Zeile." in source["data"]["content"]


def test_source_integrity_failure_is_typed_and_not_retryable(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Unveränderliche Quelle."}, scopes={"liva.write"})
    source_id = begin["data"]["source_id"]
    source_path = next((vault / "sources/captures").rglob(f"{source_id}.md"))
    source_path.write_bytes(source_path.read_bytes() + b"\nmanipuliert\n")
    result = call_tool(active, "liva_memory_source", {"source_id": source_id}, scopes={"liva.read"})
    assert result["ok"] is False
    assert result["error"]["code"] == "SOURCE_INTEGRITY_ERROR"
    assert result["error"]["details"]["retryable"] is False


def test_multiple_operations_on_one_page_preserve_each_other(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    target = vault / "wiki/Ich/Ziel.md"
    target.write_text("# Ziel\n\nVerknüpftes Ziel.\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    active = _service(service, vault, runtime)
    context = call_tool(active, "liva_memory_context", {"queries": ["langfristig planen"]}, scopes={"liva.read"})
    ref = context["data"]["results"][0]["section_ref"]
    begin = call_tool(active, "liva_memory_begin", {"text": "Planung wird konkret und mit Ziel verknüpft."}, scopes={"liva.write"})
    finish = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Planung wird konkret und mit Ziel verknüpft."], "route": "living_wiki",
        "changes": [
            {"action": "replace_section", "target_ref": ref, "text": "Die Person plant konkret."},
            {"action": "ensure_links", "target_ref": ref, "links": [{"path": "wiki/Ich/Ziel.md", "label": "Ziel"}]},
        ],
    }, scopes={"liva.write"})
    assert finish["ok"], finish
    updated = (vault / "wiki/Ich/Profil.md").read_text(encoding="utf-8")
    assert "Die Person plant konkret." in updated and "[[wiki/Ich/Ziel|Ziel]]" in updated


def test_mid_write_failure_rolls_back_files_and_search_index(tmp_path: Path, monkeypatch) -> None:
    vault, runtime = _vault(tmp_path)
    second = vault / "wiki/Ich/Zweite.md"
    second.write_text("# Zweite\n\n## Stand\n\nAlt zwei.\n", encoding="utf-8")
    store = MemoryStore(vault, runtime)
    store.wiki_reindex()
    source = store.memory_commit(_new_commit("rollback-source"))
    refs = {item["path"]: item["section_ref"] for item in store.memory_context(["langfristig planen", "Alt zwei"], "living_wiki", limit=10)["results"]}
    original_write = store._atomic_write
    calls = {"count": 0}
    def fail_second_write(path, data):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("synthetic persistence failure")
        return original_write(path, data)
    monkeypatch.setattr(store, "_atomic_write", fail_second_write)
    payload = {
        "idempotency_key": "rollback-write", "source_id": source["source_id"],
        "claims": [{"claim_id": "c1", "text": "Direkter Testkontext.", "evidence_type": "direct_user_statement", "source_section": "direct_user_context"}],
        "knowledge_operations": [
            {"operation": "modify_section", "target_ref": refs["wiki/Ich/Profil.md"], "new_text": "Neu eins.", "used_claim_ids": ["c1"]},
            {"operation": "modify_section", "target_ref": refs["wiki/Ich/Zweite.md"], "new_text": "Neu zwei.", "used_claim_ids": ["c1"]},
        ],
        "compile_result": {"state": "compiled"},
    }
    try:
        store.memory_commit(payload)
        raise AssertionError("write unexpectedly succeeded")
    except StoreError as exc:
        assert exc.code == "PERSISTENCE_ERROR"
        assert exc.details["rollback_ok"] is True
    assert "langfristig klar planen" in (vault / "wiki/Ich/Profil.md").read_text(encoding="utf-8")
    assert "Alt zwei." in second.read_text(encoding="utf-8")
    indexed = store.memory_context(["Neu eins Neu zwei"], "living_wiki")["results"]
    assert all("Neu eins" not in item["exact_text"] and "Neu zwei" not in item["exact_text"] for item in indexed)


def test_concurrent_identical_commits_produce_one_commit_and_one_replay(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    store = MemoryStore(vault, runtime)
    payload = _new_commit("concurrent-replay")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.memory_commit(payload), range(2)))
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert len(list((vault / "sources/captures").rglob("*.md"))) == 1


def test_invalid_finish_is_typed_and_does_not_break_following_read(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Test."}, scopes={"liva.write"})
    invalid = call_tool(active, "liva_memory_finish", {"source_id": begin["data"]["source_id"], "claims": ["Test."], "route": "living_wiki", "changes": [{"action": "replace_section", "target_ref": "sec_wrong", "text": "Test."}]}, scopes={"liva.write"})
    assert invalid["ok"] is False and invalid["error"]["code"] == "STALE_CONTEXT"
    assert call_tool(active, "liva_memory_recall", {"query": "langfristig planen"}, scopes={"liva.read"})["ok"] is True


def test_malformed_finish_change_is_diagnostic_not_generic_write_failure(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Test."}, scopes={"liva.write"})
    invalid = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Test."], "route": "living_wiki", "changes": [{}],
    }, scopes={"liva.write"})
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_CHANGE"
    assert "action" in invalid["error"]["message"].casefold()


def test_unexpected_memory_facade_failure_is_safe_and_diagnostic(service: ReadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    begin = call_tool(active, "liva_memory_begin", {"text": "Test."}, scopes={"liva.write"})
    monkeypatch.setattr(MemoryV2Service, "finish", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("internal detail")))
    failed = call_tool(active, "liva_memory_finish", {
        "source_id": begin["data"]["source_id"], "claims": ["Test."], "route": "living_wiki", "changes": [{"action": "no_change"}],
    }, scopes={"liva.write"}, request_id="req-memory-test")
    assert failed["ok"] is False
    assert failed["error"]["code"] == "MEMORY_FACADE_FAILURE"
    assert failed["error"]["details"] == {"phase": "facade", "retryable": False, "request_id": "req-memory-test"}
    assert "internal detail" not in str(failed)


def test_memory_runtime_has_no_external_model_or_memos_access_path() -> None:
    runtime = (Path(__file__).resolve().parents[2] / "liva_memory/interactive.py").read_text(encoding="utf-8").casefold()
    assert "qwen" not in runtime and "memos" not in runtime


def test_pending_source_and_existing_source_handlers(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    first = call_tool(active, "liva_memory_commit", _new_commit("new-source"), scopes={"liva.write"})
    assert first["ok"]
    source_id = first["data"]["source_id"]
    chunk = call_tool(active, "liva_memory_source", {"source_id": source_id, "max_chars": 80}, scopes={"liva.read"})
    assert chunk["ok"] and chunk["data"]["source_id"] == source_id
    existing = {"idempotency_key": "existing-source", "source_id": source_id, "claims": [], "knowledge_operations": [], "compile_result": {"state": "raw_only"}}
    result = call_tool(active, "liva_memory_commit", existing, scopes={"liva.write"})
    assert result["ok"]
    pending = call_tool(active, "liva_memory_pending", {}, scopes={"liva.read"})
    assert pending["ok"] and isinstance(pending["data"]["sources"], list)


def test_inbox_ingest_uses_only_vault_inbox_and_is_pending(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    inbox = vault / "_inbox"; inbox.mkdir()
    original = b"# Alte Zusammenfassung\r\n\r\nByte-identisch.\r\n"
    (inbox / "export.md").write_bytes(original)
    result = call_tool(active, "liva_memory_ingest_file", {"filename": "export.md", "title": "Export"}, scopes={"liva.write"})
    assert result["ok"]
    data = result["data"]
    assert data["state"] == "uncompiled" and not data["duplicate"] and not data["replayed"]
    assert not (inbox / "export.md").exists()
    assert (vault / data["stored_path"]).read_bytes() == original
    source = call_tool(active, "liva_memory_source", {"source_id": data["source_id"]}, scopes={"liva.read"})
    assert source["ok"] and source["data"]["claim_contract"] == {"source_sections": [{"name": "imported_content", "allowed_evidence_types": ["retrospective_synthesis", "assistant_summary", "assistant_interpretation"]}]}
    pending = call_tool(active, "liva_memory_pending", {"kind": "conversation_export"}, scopes={"liva.read"})
    assert data["source_id"] in {item["source_id"] for item in pending["data"]["sources"]}
    (inbox / "again.txt").write_bytes(original)
    replay = call_tool(active, "liva_memory_ingest_file", {"filename": "again.txt"}, scopes={"liva.write"})
    assert replay["ok"] and replay["data"]["replayed"] and replay["data"]["source_id"] == data["source_id"]
    assert not (inbox / "again.txt").exists()
    rejected = call_tool(active, "liva_memory_ingest_file", {"filename": "../export.md"}, scopes={"liva.write"})
    assert rejected["ok"] is False
    invalid_claim = call_tool(active, "liva_memory_commit", {"idempotency_key": "import-invalid-evidence", "source_id": data["source_id"], "claims": [{"claim_id": "bad", "text": "Nicht hochstufen.", "source_section": "imported_content", "evidence_type": "direct_user_statement"}], "knowledge_operations": [], "compile_result": {"state": "raw_only"}}, scopes={"liva.write"})
    assert invalid_claim["ok"] is False and invalid_claim["error"]["code"] == "CLAIM_CONTRACT_ERROR"


def test_inbox_missing_file_is_typed_before_a_transaction(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    result = call_tool(active, "liva_memory_import_file", {"filename": "__nicht_vorhanden__.md"}, scopes={"liva.write"})
    assert result == {"ok": False, "error": {"code": "FILE_NOT_FOUND", "message": "The requested file does not exist in the vault _inbox."}}
    assert not (runtime / "transactions").exists()


def test_stale_and_evidence_errors_are_mapped(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    context = call_tool(active, "liva_memory_context", {"queries": ["langfristig"]}, scopes={"liva.read"})["data"]["results"][0]
    (vault / "wiki/Ich/Profil.md").write_text("# Profil\n\n## Ziel\n\nGeänderter Text.\n", encoding="utf-8")
    MemoryStore(vault, runtime).wiki_reindex()
    stale = {"idempotency_key": "stale", "source": _new_commit("x")["source"], "claims": _new_commit("x")["claims"], "knowledge_operations": [{"operation": "modify_section", "target_ref": context["section_ref"], "new_text": "Neu.", "used_claim_ids": ["c1"]}], "compile_result": {"state": "compiled"}}
    result = call_tool(active, "liva_memory_commit", stale, scopes={"liva.write"})
    assert result["ok"] is False and result["error"]["code"] == "STALE_CONTEXT"
    bad = _new_commit("bad-evidence")
    bad["claims"][0]["evidence_type"] = "assistant_summary"
    result = call_tool(active, "liva_memory_commit", bad, scopes={"liva.write"})
    assert result["ok"] is False and result["error"]["code"] == "CLAIM_CONTRACT_ERROR"


def test_live_shape_reported_event_is_mapped_before_any_transaction(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    ref = call_tool(active, "liva_memory_context", {"queries": ["langfristig"]}, scopes={"liva.read"})
    # The live payload claimed a reported event from direct_user_context. That
    # source section is deliberately restricted to direct_user_statement.
    payload = _new_commit("live-shape-evidence")
    payload["source"]["event_date"] = "2026-08-27"
    payload["source"]["temporal_scope"] = "until_changed"
    for claim in payload["claims"]:
        claim.update({"evidence_type": "reported_event", "epistemic_status": "reported", "event_date": "2026-08-27", "temporal_scope": "historical"})
    payload["knowledge_operations"] = [{"operation": "modify_section", "target_ref": ref["data"]["results"][0]["section_ref"], "classification": "TEMPORAL_UPDATE", "new_text": "Aktualisierter Stand.", "used_claim_ids": ["c1"]}]
    payload["compile_result"] = {"routing": "living_wiki", "classification": "TEMPORAL_UPDATE", "reason": "Regression"}
    result = call_tool(active, "liva_memory_commit", payload, scopes={"liva.write"})
    assert result == {"ok": False, "error": {"code": "CLAIM_CONTRACT_ERROR", "message": "Claims are incompatible with the source evidence contract."}}
    captures = vault / "sources/captures"
    assert not captures.exists() or not list(captures.rglob("*.md"))
    assert not (runtime / "audit").exists()
    assert (vault / "wiki/Ich/Profil.md").read_text(encoding="utf-8") == "# Profil\n\n## Ziel\n\nDie Person möchte langfristig klar planen.\n"


def test_live_retry_shape_commits_in_an_isolated_vault(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    context = call_tool(active, "liva_memory_context", {"queries": ["langfristig"]}, scopes={"liva.read"})
    payload = _new_commit("chatgpt-memory-v2-live-retry-2026-08-27-v2")
    payload["source"].update({"event_date": "2026-08-27", "temporal_scope": "historical"})
    payload["claims"].append({"claim_id": "c2", "text": "Ein zweiter direkter Punkt.", "source_section": "direct_user_context", "evidence_type": "direct_user_statement", "epistemic_status": "reported", "event_date": "2026-08-27", "temporal_scope": "historical"})
    for claim in payload["claims"]:
        claim.update({"evidence_type": "direct_user_statement", "epistemic_status": "reported", "event_date": "2026-08-27", "temporal_scope": "historical"})
    payload["knowledge_operations"] = [{"operation": "modify_section", "target_ref": context["data"]["results"][0]["section_ref"], "classification": "TEMPORAL_UPDATE", "new_text": "Aktualisierter zeitlicher Stand.", "used_claim_ids": ["c1", "c2"]}]
    payload["compile_result"] = {"routing": "living_wiki", "classification": "TEMPORAL_UPDATE", "reason": "Live-retry regression"}
    result = call_tool(active, "liva_memory_commit", payload, scopes={"liva.write"})
    assert result["ok"] and result["data"]["state"] == "compiled"
    assert "Aktualisierter zeitlicher Stand." in (vault / "wiki/Ich/Profil.md").read_text(encoding="utf-8")
    assert list((vault / "sources/captures").rglob("*.md"))
    assert list((runtime / "audit").glob("*.json"))


def test_replay_conflict_and_existing_tools(service: ReadService, tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    payload = _new_commit("replay")
    first = call_tool(active, "liva_memory_commit", payload, scopes={"liva.write"})
    replay = call_tool(active, "liva_memory_commit", payload, scopes={"liva.write"})
    assert first["ok"] and replay["ok"] and replay["data"]["replayed"] is True
    conflict = _new_commit("replay")
    conflict["source"]["title"] = "Different"
    result = call_tool(active, "liva_memory_commit", conflict, scopes={"liva.write"})
    assert result["ok"] is False and result["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "liva_capture_memory" not in TOOL_BY_NAME and "liva_memory_recall" in TOOL_BY_NAME


def test_commit_cannot_bypass_core(service: ReadService, tmp_path: Path, monkeypatch) -> None:
    vault, runtime = _vault(tmp_path)
    active = _service(service, vault, runtime)
    called = {"value": False}
    original = MemoryStore.memory_commit
    def wrapped(self, payload):
        called["value"] = True
        return original(self, payload)
    monkeypatch.setattr(MemoryStore, "memory_commit", wrapped)
    assert call_tool(active, "liva_memory_commit", _new_commit("core-boundary"), scopes={"liva.write"})["ok"]
    assert called["value"]
