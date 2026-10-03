from __future__ import annotations

import json
import sqlite3
import urllib.error

import pytest

from integrations.endurance_intervals_sync import (
    MissingExecutionPaceError,
    MissingResolvedTargetError,
    WorkoutSerializationError,
    build_intervals_payload,
    external_id_for_session,
    payload_hash,
    serialize_endurance_session_to_intervals,
    run_sync_jobs,
    validate_remote_semantics,
)
from analysis.run_pace_model import execution_model_quality, execution_pace_for_hr_percent, fit_hr_pace_model
from plans.endurance_planning import create_plan, ensure_endurance_schema, patch_plan
from database import connections
from integrations.intervals_client import (
    IntervalsAuthError,
    IntervalsClient,
    IntervalsNetworkError,
    IntervalsRateLimitError,
    IntervalsServerError,
    IntervalsValidationError,
)


def step(step_id, kind, *, duration=None, distance=None, parent=None, reps=None, target=None, resolved=None, order=0):
    return {"id": step_id, "kind": kind, "duration_s": duration, "distance_m": distance, "parent_step_id": parent, "reps": reps, "target": target or {"basis": "open", "metric": "open"}, "resolved": resolved, "sort_order": order}


def session(steps):
    return {"id": "es_abc", "title": "6 × 800 m", "scheduled_date": "2026-11-03", "steps": steps}


def test_serializer_interval_repeat_absolute_pace():
    steps = [
        step("w", "warmup", duration=900, resolved={"pace_min_s_per_km": 320, "pace_max_s_per_km": 350}, target={"basis": "absolute"}),
        step("r", "repeat", reps=6, target={"basis": "open", "metric": "open"}, order=1),
        step("i", "work", distance=800, parent="r", resolved={"pace_min_s_per_km": 240, "pace_max_s_per_km": 245}, target={"basis": "absolute"}),
        step("e", "recovery", distance=400, parent="r", resolved={"pace_min_s_per_km": 330, "pace_max_s_per_km": 370}, target={"basis": "absolute"}, order=1),
        step("c", "cooldown", duration=600, resolved={"pace_s_per_km": 340}, target={"basis": "absolute"}, order=2),
    ]
    text = serialize_endurance_session_to_intervals(session(steps))
    assert "Einlaufen 15m 5:20-5:50/km Pace" in text
    assert "Main Set" in text
    assert text.count("800mtr 4:00-4:05/km Pace") == 6
    assert text.count("400mtr 5:30-6:10/km Pace") == 5
    assert "Auslaufen 10m 5:40/km Pace" in text


def test_serializer_uses_personal_execution_pace_for_internal_hr_target():
    text = serialize_endurance_session_to_intervals(session([
        step("a", "open", duration=600),
        step("b", "work", duration=300, target={"basis": "absolute", "hr_min_pct": 85, "hr_max_pct": 91}, resolved={"metric": "pace", "pace_min_s_per_km": 305, "pace_max_s_per_km": 340, "source": "current_hr_pace_model"}, order=1),
    ]))
    assert "- Locker 10m" in text
    assert "Belastung 5m 5:05-5:40/km Pace" in text


def test_serializer_rejects_hr_only_export_without_execution_pace():
    with pytest.raises(MissingExecutionPaceError):
        serialize_endurance_session_to_intervals({"id": "hill", "title": "Bergintervalle", "scheduled_date": "2026-12-01", "steps": [
            step("i", "work", duration=60, target={"basis": "absolute", "hr_min_pct": 88, "hr_max_pct": 95}),
        ]})


def test_current_hr_pace_model_excludes_non_running_sports_and_returns_pace_range():
    rows = [
        {"date": "2026-09-07", "distance": 5200, "moving_time": 1798, "avg_hr": 158, "max_hr": 179, "pace": 343, "sport_type": "run"},
        {"date": "2026-08-20", "distance": 5000, "moving_time": 1600, "avg_hr": 166, "max_hr": 184, "pace": 320, "sport_type": "run"},
        {"date": "2026-07-20", "distance": 5000, "moving_time": 1500, "avg_hr": 174, "max_hr": 190, "pace": 300, "sport_type": None},
        {"date": "2026-06-20", "distance": 5000, "moving_time": 1400, "avg_hr": 180, "max_hr": 194, "pace": 280, "sport_type": "running"},
        {"date": "2026-09-08", "distance": 12000, "moving_time": 1800, "avg_hr": 130, "max_hr": 145, "pace": 150, "sport_type": "ergo"},
    ]
    model = fit_hr_pace_model(rows)
    assert model is not None and model.sample_count == 4 and model.latest_run_date == "2026-09-07"
    target = execution_pace_for_hr_percent(model, 85, 91)
    assert target["source"] == "current_hr_pace_model"
    assert target["pace_min_s_per_km"] < target["pace_max_s_per_km"]
    assert execution_model_quality(model)["execution_ready"] is False


def test_serializer_rejects_unresolved_semantic_target_and_invalid_repeat():
    with pytest.raises(MissingResolvedTargetError):
        serialize_endurance_session_to_intervals(session([step("a", "work", duration=60, target={"basis": "fitness_anchor", "zone": "threshold"})]))
    with pytest.raises(WorkoutSerializationError):
        serialize_endurance_session_to_intervals(session([step("r", "repeat", reps=3)]))


def test_payload_is_stable_and_hash_changes_only_with_payload():
    payload = build_intervals_payload(session([step("a", "open", distance=1000)]))
    assert payload["external_id"] == external_id_for_session("es_abc")
    assert payload["category"] == "WORKOUT" and payload["type"] == "Run"
    assert payload_hash(payload) == payload_hash(dict(payload))
    changed = dict(payload, name="changed")
    assert payload_hash(payload) != payload_hash(changed)


def test_readback_rejects_changed_instruction_or_pace_boundary():
    source = session([step("work", "work", duration=60, target={"basis": "absolute"}, resolved={"pace_min_s_per_km": 300, "pace_max_s_per_km": 310})])
    source["steps"][0]["notes"] = "Kontrolliert beschleunigen"
    remote = {"workout_doc": {"duration": 60, "steps": [{"duration": 60, "intensity": "interval", "text": "Belastung – (Kontrolliert beschleunigen)", "pace": {"start": 300, "end": 310}}]}}
    validate_remote_semantics(source, remote)
    changed_text = json.loads(json.dumps(remote)); changed_text["workout_doc"]["steps"][0]["text"] = "Work"
    with pytest.raises(WorkoutSerializationError, match="Anweisung"):
        validate_remote_semantics(source, changed_text)
    changed_pace = json.loads(json.dumps(remote)); changed_pace["workout_doc"]["steps"][0]["pace"]["end"] = 311
    with pytest.raises(WorkoutSerializationError, match="Pace"):
        validate_remote_semantics(source, changed_pace)
    exact = session([step("work", "work", duration=60, target={"basis": "absolute", "pace_s_per_km": 246})])
    exact_remote = {"workout_doc": {"duration": 60, "steps": [{"duration": 60, "intensity": "interval", "text": "Belastung", "pace": {"value": 246}}]}}
    validate_remote_semantics(exact, exact_remote)


def test_serializer_keeps_clock_shaped_text_out_of_intervals_parser_syntax():
    source = session([step("recovery", "recovery", duration=150, target={"basis": "absolute", "pace_s_per_km": 420})])
    source["steps"][0]["notes"] = "Etwa 2:30 min locker traben."
    text = serialize_endurance_session_to_intervals(source)
    assert "Etwa 2∶30 min locker traben." in text
    assert "Etwa 2:30 min locker traben." not in text


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return json.dumps(self.payload).encode()


def test_client_bulk_contract(monkeypatch):
    seen = []
    def open_(req, timeout):
        seen.append(req)
        return Response([{"id": 1}])
    monkeypatch.setattr("urllib.request.urlopen", open_)
    client = IntervalsClient(api_key="secret", base_url="https://example.test/api/v1")
    client.upsert_events([{"external_id": "x"}]); client.delete_events([{"external_id": "x"}])
    assert seen[0].method == "POST" and seen[0].full_url.endswith("events/bulk?upsert=true")
    assert seen[1].method == "PUT" and seen[1].full_url.endswith("events/bulk-delete")
    assert json.loads(seen[0].data) == [{"external_id": "x"}]
    assert b"secret" not in seen[0].data


@pytest.mark.parametrize("status,cls", [(401, IntervalsAuthError), (429, IntervalsRateLimitError), (422, IntervalsValidationError), (500, IntervalsServerError)])
def test_client_error_classes(monkeypatch, status, cls):
    def fail(req, timeout):
        headers = {"Retry-After": "17"} if status == 429 else {}
        raise urllib.error.HTTPError(req.full_url, status, "bad", headers, None)
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(cls) as caught:
        IntervalsClient(api_key="x").get_events("2026-01-01", "2026-01-02")
    if status == 429:
        assert caught.value.retry_after == 17


def test_client_timeout(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("slow")))
    with pytest.raises(IntervalsNetworkError):
        IntervalsClient(api_key="x").get_events("2026-01-01", "2026-01-02")


class FakeIntervals:
    def __init__(self): self.upserts = []; self.deletes = []
    def upsert_events(self, payloads):
        self.upserts.append(payloads)
        output = []
        for index, payload in enumerate(payloads):
            steps = []
            for line in payload["description"].splitlines():
                if not line.startswith("-"): continue
                import re
                duration = re.search(r"\b(\d+)m(?:(\d+)s)?\b", line); seconds = re.search(r"\b(\d+)s\b", line); distance = re.search(r"\b(\d+)(km|mtr)\b", line)
                first = duration or seconds or distance
                item = {"text": line[1:first.start()].strip() if first else line[1:].strip(), "duration": int(duration.group(1))*60+int(duration.group(2) or 0) if duration else int(seconds.group(1)) if seconds else 0}
                if distance: item = {"text": line[1:distance.start()].strip(), "distance": int(distance.group(1))*(1000 if distance.group(2)=="km" else 1)}
                intensity = re.search(r"intensity=(\w+)", line)
                if intensity: item["intensity"] = intensity.group(1)
                pace_match = re.search(r"(\d+):(\d{2})(?:-(\d+):(\d{2}))?/km Pace", line)
                if pace_match:
                    fast=int(pace_match.group(1))*60+int(pace_match.group(2)); slow=int(pace_match.group(3))*60+int(pace_match.group(4)) if pace_match.group(3) else fast
                    item["pace"] = {"start": fast, "end": slow}
                if "% HR" in line: item["hr"] = {"start": 70}
                steps.append(item)
            output.append({**payload, "id": 1000 + index, "workout_doc": {"steps": steps, "duration": sum(s.get("duration",0) for s in steps)}})
        return output
    def delete_events(self, payloads): self.deletes.append(payloads); return len(payloads)


class OfflineIntervals(FakeIntervals):
    def upsert_events(self, payloads):
        raise IntervalsNetworkError("offline")


class OneCorruptIntervals(FakeIntervals):
    def upsert_events(self, payloads):
        output = super().upsert_events(payloads)
        output[0]["workout_doc"]["steps"][0]["text"] = "Verändert"
        return output


def test_outbox_create_update_idempotency_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "PLANS_DB", str(tmp_path / "plans.sqlite3"))
    ensure_endurance_schema()
    plan = create_plan({
        "title": "Sync test", "event": {"event_date": "2027-01-01", "distance_m": 5000, "target_time_s": 1200},
        "sessions": [{"id": "future", "scheduled_date": "2026-12-01", "session_type": "easy", "title": "Easy", "steps": [{"kind": "work", "duration_s": 1800, "target": {"basis": "open", "metric": "open"}}]}],
    })
    fake = FakeIntervals()
    first = run_sync_jobs(client=fake)
    assert first["synced"] == 1 and len(fake.upserts) == 1
    assert run_sync_jobs(client=fake)["processed"] == 0
    updated = patch_plan(plan["id"], [{"op": "update_session", "session_id": "future", "title": "Easy changed"}])["plan"]
    assert next(s for s in updated["sessions"] if s["id"] == "future")["sync_state"] == "dirty"
    assert run_sync_jobs(client=fake)["synced"] == 1 and len(fake.upserts) == 2
    patch_plan(plan["id"], [{"op": "delete_session", "session_id": "future"}], confirm=True)
    assert run_sync_jobs(client=fake)["deleted"] == 1
    assert fake.deletes == [[{"external_id": "liva:endurance:future"}]]


def test_one_bad_readback_does_not_fail_the_whole_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "PLANS_DB", str(tmp_path / "plans.sqlite3"))
    ensure_endurance_schema()
    create_plan({
        "title": "Isolated readback",
        "event": {"event_date": "2027-01-01"},
        "sessions": [
            {"id": "bad", "scheduled_date": "2026-12-01", "session_type": "easy", "title": "Bad", "steps": [{"kind": "work", "duration_s": 600, "target": {"basis": "open", "metric": "open"}}]},
            {"id": "good", "scheduled_date": "2026-12-02", "session_type": "easy", "title": "Good", "steps": [{"kind": "work", "duration_s": 600, "target": {"basis": "open", "metric": "open"}}]},
        ],
    })
    result = run_sync_jobs(client=OneCorruptIntervals())
    assert result["synced"] == 1 and result["errors"] == 1
    conn = sqlite3.connect(connections.PLANS_DB)
    assert conn.execute("SELECT sync_state FROM endurance_sessions WHERE id='bad'").fetchone()[0] == "sync_error"
    assert conn.execute("SELECT sync_state FROM endurance_sessions WHERE id='good'").fetchone()[0] == "synced"
    conn.close()


def test_intervals_outage_keeps_local_plan_and_retry_job(tmp_path, monkeypatch):
    db = tmp_path / "plans.sqlite3"; monkeypatch.setattr(connections, "PLANS_DB", str(db)); ensure_endurance_schema()
    plan = create_plan({"title": "Offline", "event": {"event_date": "2027-01-01"}, "sessions": [{"id": "safe", "scheduled_date": "2026-12-01", "session_type": "easy", "title": "Easy", "steps": [{"kind": "work", "duration_s": 1200, "target": {"basis": "open", "metric": "open"}}]}]})
    result = run_sync_jobs(client=OfflineIntervals())
    assert result["errors"] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT title FROM endurance_plans WHERE id=?", (plan["id"],)).fetchone()[0] == "Offline"
    assert conn.execute("SELECT status FROM endurance_sync_jobs WHERE session_id='safe'").fetchone()[0] == "retry"
