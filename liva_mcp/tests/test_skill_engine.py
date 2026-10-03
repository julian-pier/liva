from __future__ import annotations

import json
from pathlib import Path

import pytest

from liva_memory import MemoryStore
from liva_mcp.config import Config
from liva_mcp.memory_v2_service import MemoryV2Service, MemoryV2ToolError
from liva_mcp.read_service import ReadService
from liva_mcp.skill_engine import SkillEngineError
from liva_mcp.tool_registry import call_tool, list_tools


WRITE = {"liva.read", "liva.write"}


def _isolated(service: ReadService, tmp_path: Path) -> tuple[ReadService, Path, Path]:
    vault = tmp_path / "vault"
    runtime = tmp_path / "runtime"
    skill = vault / "procedures/skills/liva-grill-me"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: liva-grill-me\ndescription: Test\n---\n\n# Grill Me\n\nStelle genau eine adaptive Frage.\n",
        encoding="utf-8",
    )
    (skill / "LIVA-OVERLAY.md").write_text(
        "# Overlay\n\nPersistiere jeden beantworteten Zug.\n",
        encoding="utf-8",
    )
    hub = vault / "wiki/Projekte/Testprojekt/Testprojekt.md"
    hub.parent.mkdir(parents=True)
    hub.write_text(
        '---\ntitle: "Testprojekt"\ntype: hub\ncssclasses:\n  - liva-wiki\n---\n\n# Testprojekt\n\nSynthetischer Test-Hub.\n',
        encoding="utf-8",
    )
    store = MemoryStore(vault, runtime)
    store.wiki_reindex()
    store.procedure_reindex()
    service.config = Config(
        repo_root=service.config.repo_root,
        database_root=service.config.database_root,
        memory_vault=vault,
        memory_runtime_dir=runtime,
    )
    service._skills = None
    return service, vault, runtime


def _act(service: ReadService, command: str, payload: dict, *, client_id: str = "client-a") -> dict:
    return call_tool(
        service,
        "liva_coach_act",
        {"domain": "skill", "command": command, "payload": payload},
        scopes=WRITE,
        client_id=client_id,
    )


def _start(service: ReadService, topic: str, key: str, topic_type: str = "technical", *, client_id: str = "client-a") -> dict:
    result = _act(service, "start", {"skill_id": "grill-me", "topic": topic, "topic_type": topic_type, "idempotency_key": key}, client_id=client_id)
    assert result["ok"], result
    return result["data"]


def _turn(service: ReadService, session: dict, number: int, *, milestone: bool = False) -> dict:
    result = _act(service, "record_turn", {
        "skill_session_id": session["skill_session_id"],
        "expected_revision": session["revision"],
        "question": f"Frage {number}?",
        "answer": f"Antwort {number}",
        "decisions": [f"Entscheidung {number}"] if milestone else [],
        "open_questions": ["Was fehlt noch?"],
        "progress": f"Fortschritt {number}",
        "milestone": milestone,
        "idempotency_key": f"turn-{session['skill_session_id']}-{number}",
    })
    assert result["ok"], result
    return result["data"]


def test_public_manifest_stays_on_evergreen_tool_pair() -> None:
    names = {entry["name"] for entry in list_tools()}
    assert "liva_coach_read" in names and "liva_coach_act" in names
    assert not any(name.startswith("liva_skill_") for name in names)


def test_live_capabilities_publish_skill_domain_without_changing_tool_names(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)

    class Backend:
        def read(self, mode, payload):
            assert mode == "capabilities"
            return {"result": {"contract_revision": "backend-7", "read_modes": ["capabilities"], "commands": {"training": {}}}}

    service.actions = Backend()
    result = call_tool(service, "liva_coach_read", {"mode": "capabilities", "payload": {}}, scopes={"liva.read"})
    assert result["ok"]
    contract = result["data"]["result"]
    assert contract["contract_revision"] == "backend-7"
    assert contract["skill_engine"]["contract_revision"] == "skill-engine-2"
    assert contract["commands"]["skill"]["start"]["required"] == ["skill_id", "topic", "idempotency_key"]
    assert {"skill_registry", "skill_load", "skill_session"} <= set(contract["read_modes"])


def test_technical_and_personal_grilling_load_original_method(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)
    technical = _start(service, "Eine technische Deployment-Idee", "start-tech", "technical")
    personal = _start(service, "Eine persönliche Entscheidung", "start-personal", "personal")

    assert technical["skill"]["methodology"].endswith("Stelle genau eine adaptive Frage.\n")
    assert technical["turn_contract"]["question_count"] == 1
    assert "structured" in technical["turn_contract"]["style"]
    assert "natural" in personal["turn_contract"]["style"]
    assert technical["skill"]["version"] == technical["skill_version"] if "skill_version" in technical else technical["skill"]["version"]
    assert technical["persistence"]["verified"] is True


def test_skill_loader_returns_complete_methodology_not_generic_preview(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    body = "# Grill Me\n\n" + ("Eine konkrete Methodenzeile.\n" * 80)
    (vault / "procedures/skills/liva-grill-me/SKILL.md").write_text(body, encoding="utf-8")
    result = call_tool(service, "liva_coach_read", {"mode": "skill_load", "payload": {"skill_id": "grill-me"}}, scopes={"liva.read"})
    assert result["ok"]
    assert result["data"]["methodology"] == body
    assert len(result["data"]["methodology"]) > 500


def test_multiple_answers_persist_and_auto_checkpoint(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _start(service, "Persistente technische Idee", "start-persist")
    session = _turn(service, session, 1)
    session = _turn(service, session, 2)
    session = _turn(service, session, 3)

    assert session["turn_count"] == 3
    assert session["checkpoint"]["verified"] is True
    active = vault / "sessions/active" / f"{session['skill_session_id']}.json"
    stored = json.loads(active.read_text(encoding="utf-8"))
    assert len(stored["turns"]) == 3
    assert stored["progress"] == "Fortschritt 3"


def test_successive_automatic_checkpoints_store_only_new_turns(service: ReadService, tmp_path: Path) -> None:
    service, vault, runtime = _isolated(service, tmp_path)
    session = _start(service, "Inkrementelle Idee", "start-incremental")
    for number in range(1, 7):
        session = _turn(service, session, number)

    with service.skills._connect() as db:
        checkpoints = db.execute(
            "SELECT source_path FROM skill_checkpoints WHERE session_id=? ORDER BY checkpoint_id",
            (session["skill_session_id"],),
        ).fetchall()
    assert len(checkpoints) == 2
    first = (vault / checkpoints[0]["source_path"]).read_text(encoding="utf-8")
    second = (vault / checkpoints[1]["source_path"]).read_text(encoding="utf-8")
    assert "Antwort 1" in first and "Antwort 3" in first
    assert "Antwort 4" in second and "Antwort 6" in second
    assert "Antwort 1" not in second and "Antwort 3" not in second

    events = service.skills._connect().execute(
        "SELECT event_type, turn_number FROM skill_events WHERE session_id=? ORDER BY event_id",
        (session["skill_session_id"],),
    ).fetchall()
    assert [(row["event_type"], row["turn_number"]) for row in events if row["event_type"] == "turn_recorded"] == [
        ("turn_recorded", number) for number in range(1, 7)
    ]


def test_manual_checkpoint_is_verified_and_idempotent(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Checkpoint-Idee", "start-checkpoint"), 1)
    payload = {"skill_session_id": session["skill_session_id"], "expected_revision": session["revision"], "reason": "/checkpoint", "idempotency_key": "manual-checkpoint"}
    first = _act(service, "checkpoint", payload)
    second = _act(service, "checkpoint", payload)

    assert first["ok"] and first["data"]["checkpoint"]["verified"] is True
    assert second["ok"] and second["data"]["replayed"] is True and second["data"]["changed"] is False


def test_repeated_checkpoint_with_new_key_does_not_create_another_source(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Checkpoint ohne neue Fakten", "start-noop-checkpoint"), 1)
    first = _act(service, "checkpoint", {"skill_session_id": session["skill_session_id"], "expected_revision": session["revision"], "idempotency_key": "checkpoint-first"})
    assert first["ok"]
    second = _act(service, "checkpoint", {"skill_session_id": session["skill_session_id"], "expected_revision": first["data"]["revision"], "idempotency_key": "checkpoint-second"})
    assert second["ok"] and second["data"]["no_new_information"] is True
    assert second["data"]["changed"] is False
    assert second["data"]["checkpoint"]["source_id"] == first["data"]["checkpoint"]["source_id"]
    with service.skills._connect() as db:
        count = db.execute("SELECT count(*) FROM skill_checkpoints WHERE session_id=?", (session["skill_session_id"],)).fetchone()[0]
    assert count == 1


def test_cross_client_resume_needs_no_original_chat(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Chatübergreifendes Vorhaben", "start-cross", client_id="old-chat"), 1)
    service._skills = None  # simulate a fresh process/client context
    resumed = _act(service, "resume", {"skill_session_id": session["skill_session_id"], "idempotency_key": "resume-new-chat"}, client_id="new-chat")

    assert resumed["ok"]
    assert resumed["data"]["turns"][0]["answer"] == "Antwort 1"
    assert resumed["data"]["revision"] == session["revision"] + 1
    assert resumed["data"]["turn_contract"]["next_action"] == "ask_one_question"


def test_standalone_wiki_commit_succeeds_during_active_skill_session(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    _start(service, "Aktive Sitzung", "start-active-memory-guard", client_id="same-chat")
    begun = call_tool(service, "liva_memory_begin", {"text": "Zwischenstand ohne semantischen Abschluss."}, scopes=WRITE, client_id="same-chat")
    assert begun["ok"]
    path = "wiki/Projekte/Testprojekt/Unabhängiger Zwischenstand.md"
    finished = call_tool(service, "liva_memory_finish", {
        "source_id": begun["data"]["source_id"], "claims": ["Zwischenstand ohne semantischen Abschluss."],
        "route": "living_wiki", "changes": [{
            "action": "create_page", "path": path, "title": "Unabhängiger Zwischenstand", "note_type": "topic",
            "text": "Darf eine unabhängige aktive Skill-Session nicht blockieren.",
            "links": [{"path": "wiki/Projekte/Testprojekt/Testprojekt.md", "label": "Testprojekt"}],
        }],
    }, scopes=WRITE, client_id="same-chat")
    assert finished["ok"], finished
    assert finished["data"]["reporting"]["may_claim_knowledge_saved"] is True
    assert (vault / path).is_file()


def test_running_session_remains_pinned_to_original_skill_version(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _start(service, "Versionierte Idee", "start-version")
    original_version = session["skill"]["version"]
    (vault / "procedures/skills/liva-grill-me/SKILL.md").write_text("# Neue Methodik\n", encoding="utf-8")
    resumed = _act(service, "resume", {"skill_session_id": session["skill_session_id"], "idempotency_key": "resume-version"})
    assert resumed["ok"]
    assert resumed["data"]["skill"]["version"] == original_version
    assert "pinned" in resumed["data"]["skill"]["version_notice"]


def test_ambiguous_resume_returns_candidates_without_merging(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)
    _start(service, "Atlas mobile Variante", "atlas-a")
    _start(service, "Atlas Server Variante", "atlas-b")
    result = _act(service, "resume", {"query": "Atlas", "idempotency_key": "resume-atlas"})

    assert result["ok"] is False
    assert result["error"]["code"] == "AMBIGUOUS_SKILL_SESSION"
    assert len(result["error"]["details"]["candidates"]) == 2


def test_pause_and_return_restore_exact_progress(service: ReadService, tmp_path: Path) -> None:
    service, _, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Pausierbare Idee", "start-pause"), 1)
    paused = _act(service, "pause", {"skill_session_id": session["skill_session_id"], "expected_revision": session["revision"], "reason": "Subskill", "subskill": "teach", "idempotency_key": "pause-one"})
    assert paused["ok"] and paused["data"]["status"] == "paused"
    resumed = _act(service, "resume", {"skill_session_id": session["skill_session_id"], "idempotency_key": "return-one"})
    assert resumed["ok"] and resumed["data"]["status"] == "active"
    assert resumed["data"]["turn_count"] == 1
    assert resumed["data"]["progress"] == "Fortschritt 1"


def test_checkpoint_failure_is_retryable_and_not_false_success(service: ReadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Retry-Idee", "start-retry"), 1)
    engine = service.skills
    original = engine._persist_checkpoint
    monkeypatch.setattr(engine, "_persist_checkpoint", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    payload = {"skill_session_id": session["skill_session_id"], "expected_revision": session["revision"], "idempotency_key": "retry-checkpoint"}
    failed = _act(service, "checkpoint", payload)
    assert failed["ok"] is False and failed["error"]["code"] == "CHECKPOINT_FAILED"
    assert failed["error"]["details"]["retryable"] is True

    monkeypatch.setattr(engine, "_persist_checkpoint", original)
    retried = _act(service, "checkpoint", payload)
    assert retried["ok"] and retried["data"]["checkpoint"]["verified"] is True
    assert retried["data"]["replayed"] is False


def test_turn_continues_when_vault_mirror_temporarily_fails(service: ReadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _ = _isolated(service, tmp_path)
    session = _start(service, "Offline fortsetzbare Idee", "start-mirror")
    monkeypatch.setattr(service.skills, "_write_active", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("vault offline")))
    recorded = _turn(service, session, 1)
    assert recorded["persistence"]["verified"] is False
    assert recorded["warning"]["code"] == "SESSION_MIRROR_PENDING"
    status = service.skills.status({"skill_session_id": session["skill_session_id"]})
    assert status["session"]["turn_count"] == 1


def test_stop_creates_handoff_archives_only_after_verified_memory(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Abschluss-Idee", "start-stop"), 1, milestone=True)
    stopped = _act(service, "stop", {
        "skill_session_id": session["skill_session_id"],
        "expected_revision": session["revision"],
        "handoff": {"main_idea": "Eine geprüfte Idee", "possible_implementations": ["Variante A"], "open_points": ["Kosten"], "current_status": "Grilling abgeschlossen"},
        "idempotency_key": "stop-one",
    })

    assert stopped["ok"] and stopped["data"]["may_claim_completed"] is True
    assert stopped["data"]["final_checkpoint"]["verified"] is True
    assert stopped["data"]["archive"]["verified"] is True
    assert not (vault / "sessions/active" / f"{session['skill_session_id']}.json").exists()
    assert (vault / stopped["data"]["archive"]["path"]).is_file()


def test_substantial_stop_requires_semantic_wiki_event_and_remains_resumable(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _start(service, "Substanzielles offenes Grilling", "start-substantial")
    for number in range(1, 4):
        session = _turn(service, session, number)

    stopped = _act(service, "stop", {
        "skill_session_id": session["skill_session_id"],
        "expected_revision": session["revision"],
        "handoff": {"main_idea": "Mehrere Optionen wurden geprüft", "possible_implementations": ["A", "B"], "open_points": ["Kosten"], "current_status": "Ergebnisoffen"},
        "idempotency_key": "stop-substantial-without-event",
    })

    assert stopped["ok"] is False
    assert stopped["error"]["code"] == "SEMANTIC_COMPLETION_REQUIRED"
    completion = stopped["error"]["details"]["completion_status"]
    assert completion == {
        "session_saved": True,
        "final_checkpoint_confirmed": False,
        "wiki_gardening": "not_started",
        "event_file": {"required": True, "written": False, "read_back": False, "path": None},
        "archived": False,
        "complete": False,
    }
    current = service.skills.status({"skill_session_id": session["skill_session_id"]})["session"]
    assert current["status"] == "active"
    assert (vault / "sessions/active" / f"{session['skill_session_id']}.json").is_file()


def test_substantial_open_ended_stop_writes_reads_event_and_reports_each_stage(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _start(service, "Ergebnisoffene Entscheidung", "start-open-event")
    for number in range(1, 4):
        session = _turn(service, session, number)
    event_path = "wiki/Projekte/Testprojekt/2026-09-21 – Ergebnisoffene Entscheidung.md"
    payload = {
        "skill_session_id": session["skill_session_id"], "expected_revision": session["revision"],
        "handoff": {"main_idea": "Zwei Optionen wurden geprüft", "possible_implementations": ["A", "B"], "open_points": ["Kosten"], "current_status": "Keine Entscheidung getroffen"},
        "completion": {"classification": "substantial", "rationale": "Drei inhaltliche Turns mit dauerhaft relevantem Entscheidungsstand."},
        "durable_memory": {
            "confirmed": True,
            "claims": ["Zwei Optionen wurden geprüft.", "Es wurde noch keine Entscheidung getroffen."],
            "route": "living_wiki",
            "changes": [{
                "action": "create_page", "path": event_path, "title": "Ergebnisoffene Entscheidung",
                "note_type": "event", "event_date": "2026-09-21",
                "text": "## Anlass\n\nZwei Optionen wurden geprüft.\n\n## Ergebnis\n\nEs wurde noch keine Entscheidung getroffen.",
                "links": [{"path": "wiki/Projekte/Testprojekt/Testprojekt.md", "label": "Testprojekt"}],
            }],
        },
        "idempotency_key": "stop-open-event",
    }
    stopped = _act(service, "stop", payload)
    assert stopped["ok"], stopped
    status = stopped["data"]["completion_status"]
    assert status == {
        "session_saved": True,
        "final_checkpoint_confirmed": True,
        "wiki_gardening": "succeeded",
        "event_file": {"required": True, "written": True, "read_back": True, "path": event_path},
        "archived": True,
        "complete": True,
    }
    event = (vault / event_path).read_text(encoding="utf-8")
    assert "noch keine Entscheidung" in event
    assert stopped["data"]["final_checkpoint"]["source_id"] in event
    replay = _act(service, "stop", payload)
    assert replay["ok"] and replay["data"]["replayed"] is True
    assert len(list((vault / "wiki/Projekte/Testprojekt").glob("*Ergebnisoffene Entscheidung*.md"))) == 1


def test_wiki_failure_reports_partial_status_and_keeps_session_resumable(service: ReadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _start(service, "Wiki-Fehler", "start-wiki-failure")
    for number in range(1, 4):
        session = _turn(service, session, number)
    monkeypatch.setattr(MemoryV2Service, "finish", lambda *_args, **_kwargs: (_ for _ in ()).throw(MemoryV2ToolError("WIKI_OFFLINE", "offline", {"retryable": True})))
    result = _act(service, "stop", {
        "skill_session_id": session["skill_session_id"], "expected_revision": session["revision"],
        "handoff": {"main_idea": "Test", "possible_implementations": [], "open_points": ["Retry"], "current_status": "Offen"},
        "completion": {"classification": "substantial", "rationale": "Drei Turns."},
        "durable_memory": {"confirmed": True, "claims": ["Test"], "changes": [{
            "action": "create_page", "path": "wiki/Projekte/Testprojekt/2026-09-21 – Wiki-Fehler.md",
            "title": "Wiki-Fehler", "note_type": "event", "event_date": "2026-09-21", "text": "Offen.",
            "links": [{"path": "wiki/Projekte/Testprojekt/Testprojekt.md", "label": "Testprojekt"}],
        }]},
        "idempotency_key": "stop-wiki-failure",
    })
    assert result["ok"] is False and result["error"]["code"] == "DURABLE_MEMORY_FAILED"
    status = result["error"]["details"]["completion_status"]
    assert status["session_saved"] and status["final_checkpoint_confirmed"]
    assert status["wiki_gardening"] == "failed" and status["archived"] is False and status["complete"] is False
    assert service.skills.status({"skill_session_id": session["skill_session_id"]})["session"]["status"] == "active"
    assert (vault / "sessions/active" / f"{session['skill_session_id']}.json").is_file()


def test_stop_can_integrate_confirmed_wiki_result_with_provenance(service: ReadService, tmp_path: Path) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _turn(service, _start(service, "Wiki-Idee", "start-wiki"), 1)
    stopped = _act(service, "stop", {
        "skill_session_id": session["skill_session_id"],
        "expected_revision": session["revision"],
        "handoff": {"main_idea": "Die Wiki-Idee ist bestätigt", "possible_implementations": [], "open_points": [], "current_status": "Entschieden"},
        "durable_memory": {
            "confirmed": True,
            "claims": ["Die Wiki-Idee ist bestätigt."],
            "route": "living_wiki",
            "changes": [{
                "action": "create_page", "path": "wiki/Projekte/Testprojekt/Wiki-Idee.md", "title": "Wiki-Idee",
                "text": "Die bestätigte Idee wird als eigenständiges Thema weitergeführt.", "note_type": "topic",
                "links": [{"path": "wiki/Projekte/Testprojekt/Testprojekt.md", "label": "Testprojekt"}],
            }],
        },
        "idempotency_key": "stop-wiki",
    })

    assert stopped["ok"], stopped
    memory = stopped["data"]["durable_memory"]
    assert memory["reporting"]["may_claim_knowledge_saved"] is True
    path = vault / "wiki/Projekte/Testprojekt/Wiki-Idee.md"
    text = path.read_text(encoding="utf-8")
    assert "## Quellen" in text and stopped["data"]["final_checkpoint"]["source_id"] in text
    assert "[[wiki/Projekte/Testprojekt/Testprojekt|Testprojekt]]" in text
    assert len(list((vault / "wiki/Projekte/Testprojekt").glob("*Wiki-Idee*.md"))) == 1


def test_stop_does_not_archive_when_final_persistence_fails(service: ReadService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, vault, _ = _isolated(service, tmp_path)
    session = _start(service, "Fehlerhafter Abschluss", "start-fail-stop")
    monkeypatch.setattr(service.skills, "_persist_checkpoint", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    result = _act(service, "stop", {
        "skill_session_id": session["skill_session_id"], "expected_revision": session["revision"],
        "handoff": {"main_idea": "Nicht archivieren", "possible_implementations": [], "open_points": [], "current_status": "Offen"},
        "idempotency_key": "stop-failure",
    })
    assert result["ok"] is False and result["error"]["code"] == "FINAL_PERSISTENCE_FAILED"
    status = call_tool(service, "liva_coach_read", {"mode": "skill_session", "payload": {"skill_session_id": session["skill_session_id"]}}, scopes={"liva.read"})
    assert status["ok"] and status["data"]["session"]["status"] == "active"
    assert not (vault / "sessions/archive" / f"{session['skill_session_id']}.json").exists()
