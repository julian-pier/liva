from __future__ import annotations

import json
import os
import gzip
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


ROOT = Path("/opt/liva")
FETCH_SCRIPT = ROOT / "scripts" / "webuntis_fetch_today.py"
CALENDAR_SYNC_SCRIPT = ROOT / "scripts" / "google_calendar_sync_schoolsync.py"
LATEST_JSON = ROOT / "var" / "schoolsync" / "latest_today.json"
STATE_FILE = ROOT / "var" / "schoolsync" / "webuntis_alert_state.json"
LOCK_FILE = ROOT / "var" / "locks" / "webuntis_update_watch.lock"
LOG_FILE = ROOT / "logs" / "webuntis_update_watch.log"
DEFAULT_MIN_INTERVAL_MINUTES = 10
DEFAULT_FAILURE_ALERT_GRACE_MINUTES = 30
ALERT_RETENTION_DAYS = 21
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LIVA_NOTIFY_BIN = Path("/var/lib/liva/bin/liva-notify")


@dataclass(frozen=True)
class AlertClassification:
    type: str
    is_alert: bool
    reason: str
    display_text: str


@dataclass(frozen=True)
class AlertCandidate:
    key: str
    date_iso: str
    alert_type: str
    display_text: str


def _read_env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default


def _has_recent_success(state: dict, *, now: datetime, grace_minutes: int) -> bool:
    raw = str(state.get("last_success_at") or "").strip()
    if not raw or grace_minutes <= 0:
        return False
    try:
        last_success = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    return now - last_success < timedelta(minutes=grace_minutes)
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"seen": {}, "last_run": None, "last_success_at": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": {}, "last_run": None, "last_success_at": None}
    seen = payload.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    return {
        "seen": seen,
        "last_run": payload.get("last_run"),
        "last_success_at": payload.get("last_success_at"),
    }


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _rotate_log_if_needed() -> bool:
    max_bytes = max(1024, _read_env_int("WEBUNTIS_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size < max_bytes:
            return False
    except OSError:
        return False

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    oldest = LOG_FILE.with_name(f"{LOG_FILE.name}.{LOG_BACKUP_COUNT}.gz")
    oldest.unlink(missing_ok=True)
    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        source = LOG_FILE.with_name(f"{LOG_FILE.name}.{index}.gz")
        if source.exists():
            source.replace(LOG_FILE.with_name(f"{LOG_FILE.name}.{index + 1}.gz"))

    compressed = LOG_FILE.with_name(f"{LOG_FILE.name}.1.gz")
    temporary = LOG_FILE.with_name(f".{LOG_FILE.name}.rotate.tmp.gz")
    try:
        with LOG_FILE.open("rb") as source, gzip.open(temporary, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        temporary.replace(compressed)
        LOG_FILE.write_text("", encoding="utf-8")
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _append_log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_needed()
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def _run_python(script: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["HOME"] = "/var/lib/liva"
    env["PLAYWRIGHT_BROWSERS_PATH"] = "/var/lib/liva/.cache/ms-playwright"
    env["XDG_CACHE_HOME"] = "/var/lib/liva/.cache"
    proc = subprocess.run(
        [str(ROOT / "venv" / "bin" / "python"), str(script)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if output:
        _append_log(output)
    return int(proc.returncode), output


def _run_fetch_with_reauth() -> tuple[int, str]:
    rc, output = _run_python(FETCH_SCRIPT)
    for auth_attempt in range(1, 4):
        if rc != 2 or "Login state invalid" not in output:
            return rc, output
        _append_log("WebUntis auth abgelaufen, Bootstrap wird versucht.")
        boot = ROOT / "scripts" / "webuntis_auth_bootstrap.py"
        boot_proc = subprocess.run(
            [str(ROOT / "venv" / "bin" / "python"), str(boot), "--headless", "--max-wait-seconds", "180"],
            cwd=str(ROOT),
            env={
                **os.environ,
                "HOME": "/var/lib/liva",
                "PLAYWRIGHT_BROWSERS_PATH": "/var/lib/liva/.cache/ms-playwright",
                "XDG_CACHE_HOME": "/var/lib/liva/.cache",
            },
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        boot_output = ((boot_proc.stdout or "") + "\n" + (boot_proc.stderr or "")).strip()
        if boot_output:
            _append_log(boot_output)
        if boot_proc.returncode != 0:
            return int(boot_proc.returncode), boot_output or output
        rc, output = _run_python(FETCH_SCRIPT)
        if rc == 0:
            return rc, output
        _append_log(f"WebUntis Re-Login-Versuch {auth_attempt}/3 war noch nicht nutzbar.")
    return rc, output


def _parse_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def _weekday_label(target: date) -> str:
    names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    return names[target.weekday()]


def _date_phrase(today: date, target: date) -> str:
    if target == today:
        return "Heute"
    if target == today + timedelta(days=1):
        return "Morgen"
    return _weekday_label(target)


def _lesson_phrase(entry: dict) -> str:
    start = str(entry.get("start_time") or "").strip()
    end = str(entry.get("end_time") or "").strip()
    if start and end:
        return f"{start}-{end} Uhr"
    if start:
        return f"ab {start} Uhr"
    return ""


def _subject_phrase(entry: dict) -> str:
    subject = str(entry.get("subject") or "").strip()
    return subject or "Eine Unterrichtsstunde"


def _normalize_text(value: str | None) -> str:
    return str(value or "").strip().lower().replace("fällt", "faellt")


def classify_webuntis_change(entry: dict, *, today: date | None = None) -> AlertClassification:
    today_ref = today or date.today()
    target_date = _parse_date(str(entry.get("date") or "")) or today_ref
    blob = " | ".join(
        str(entry.get(field) or "")
        for field in ("status_hint", "raw_text", "subject", "teacher", "room", "info", "text", "type", "code")
    )
    text = _normalize_text(blob)
    status = _normalize_text(entry.get("status_hint"))
    cancelled_tokens = (
        "entfall",
        "ausfall",
        "faellt aus",
        "stunde entfaellt",
        "unterricht entfaellt",
        "vertretung entfaellt",
        "cancelled",
        "canceled",
        "absent",
        "no lesson",
    )
    free_tokens = ("freistunde", " frei ", " free ", "unterrichtsfrei")
    changed_tokens = ("vertretung", "substitution", "changed", "geaendert", "raumwechsel")

    if status in {"cancelled", "eva"} or any(token in text for token in cancelled_tokens):
        when = _date_phrase(today_ref, target_date)
        lesson = _lesson_phrase(entry)
        subject = _subject_phrase(entry)
        detail = f"{when}, {lesson}: {subject} fällt aus." if lesson else f"{when}: {subject} fällt aus."
        return AlertClassification("entfall", True, "Entfall erkannt", detail)
    if any(token in text for token in free_tokens):
        when = _date_phrase(today_ref, target_date)
        lesson = _lesson_phrase(entry)
        subject = _subject_phrase(entry)
        detail = f"{when}, {lesson}: {subject} ist frei." if lesson else f"{when}: {subject} ist frei."
        return AlertClassification("freistunde", True, "Freistunde", detail)
    if status == "changed" or any(token in text for token in changed_tokens):
        return AlertClassification("vertretung", False, "Änderung", "")
    return AlertClassification("sonstiges", False, "", "")


def _entry_key(entry: dict, classification: AlertClassification) -> str:
    return "|".join(
        [
            str(entry.get("date") or ""),
            str(entry.get("start_time") or ""),
            str(entry.get("end_time") or ""),
            str(entry.get("subject") or ""),
            str(entry.get("teacher") or ""),
            str(entry.get("room") or ""),
            classification.type,
            str(entry.get("status_hint") or ""),
        ]
    )


def _entry_starts_in_future(entry: dict, *, now: datetime) -> bool:
    target_date = _parse_date(str(entry.get("date") or ""))
    if target_date is None:
        return False
    start_raw = str(entry.get("start_time") or "").strip()
    if target_date > now.date():
        return True
    if target_date < now.date():
        return False
    if not start_raw:
        return False
    try:
        start_dt = datetime.fromisoformat(f"{target_date.isoformat()}T{start_raw}:00")
    except Exception:
        return False
    return start_dt > now


def _extract_alert_candidates(payload: dict, *, now: datetime | None = None, today: date | None = None) -> list[AlertCandidate]:
    now_ref = now or datetime.now().replace(microsecond=0)
    today_ref = today or now_ref.date()
    entries = payload.get("entries_visible") or payload.get("entries") or []
    out: list[AlertCandidate] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        classification = classify_webuntis_change(entry, today=today_ref)
        if not classification.is_alert:
            continue
        if not _entry_starts_in_future(entry, now=now_ref):
            continue
        out.append(
            AlertCandidate(
                key=_entry_key(entry, classification),
                date_iso=str(entry.get("date") or "").strip(),
                alert_type=classification.type,
                display_text=classification.display_text,
            )
        )
    return out


def _prune_seen(seen: dict[str, str], *, now: datetime) -> dict[str, str]:
    cutoff = now - timedelta(days=ALERT_RETENTION_DAYS)
    out: dict[str, str] = {}
    for key, stamp in seen.items():
        try:
            ts = datetime.fromisoformat(str(stamp))
        except Exception:
            continue
        if ts >= cutoff:
            out[str(key)] = ts.isoformat()
    return out


def _ntfy_topic() -> str:
    return str(os.getenv("NTFY_TOPIC") or os.getenv("WEBUNTIS_NTFY_TOPIC") or "").strip()


def _ntfy_base_url() -> str:
    return str(os.getenv("NTFY_BASE_URL") or "https://ntfy.sh").strip().rstrip("/")


def _ntfy_ready() -> bool:
    return LIVA_NOTIFY_BIN.exists() or bool(_ntfy_topic())


def _send_school_alert(text: str) -> bool:
    text = str(text or "").strip()
    if not text:
        return False
    title = "Schule: Änderung"
    if "fällt aus" in text:
        title = "Schule: Entfall erkannt"
    elif "ist frei" in text:
        title = "Schule: Freistunde"
    if not _ntfy_ready():
        return False
    if LIVA_NOTIFY_BIN.exists():
        try:
            env = os.environ.copy()
            env.setdefault("LIVA_NOTIFY_TITLE", f"LIVA · {title}")
            env.setdefault("LIVA_NOTIFY_PRIORITY", "4")
            env.setdefault("LIVA_NOTIFY_TAGS", "school,warning")
            proc = subprocess.run(
                [str(LIVA_NOTIFY_BIN), text],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                return True
            _append_log(f"liva-notify failed with exit code {proc.returncode}")
        except Exception as exc:
            _append_log(f"liva-notify failed: {exc}")
    topic = _ntfy_topic()
    if not topic:
        return False
    url = f"{_ntfy_base_url()}/{topic}"
    body = text.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Title": title,
            "Priority": "default",
            "Tags": "school",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    token = str(os.getenv("NTFY_TOKEN") or os.getenv("WEBUNTIS_NTFY_TOKEN") or "").strip()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = int(getattr(resp, "status", 0) or 0)
            return 200 <= status < 300
    except urllib.error.URLError as exc:
        _append_log(f"ntfy send failed: {exc}")
        return False
    except Exception as exc:
        _append_log(f"ntfy send failed: {exc}")
        return False


def _tail(text: str, lines: int = 6) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    return "\n".join(raw.splitlines()[-lines:])


def _lock_is_active(now: datetime) -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        payload = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(str(payload.get("started_at") or ""))
    except Exception:
        return False
    return now - ts < timedelta(minutes=30)


def _write_lock(now: datetime) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.write_text(json.dumps({"started_at": now.isoformat()}), encoding="utf-8")


def _clear_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> int:
    now = datetime.now().replace(microsecond=0)
    state = _load_state(STATE_FILE)
    min_interval = _read_env_int("WEBUNTIS_CHECK_MIN_INTERVAL_MINUTES", DEFAULT_MIN_INTERVAL_MINUTES)

    if _lock_is_active(now):
        print("WebUntis-Check übersprungen: bereits ein Lauf aktiv.")
        return 0

    last_success_raw = str(state.get("last_success_at") or "").strip()
    force_run = str(os.getenv("WEBUNTIS_CHECK_FORCE") or "").strip().lower() in {"1", "true", "yes"}
    if last_success_raw and min_interval > 0 and not force_run:
        try:
            last_success = datetime.fromisoformat(last_success_raw)
        except Exception:
            last_success = None
        if last_success and now - last_success < timedelta(minutes=min_interval):
            print(f"WebUntis-Check übersprungen: letzter Lauf vor weniger als {min_interval} Minuten.")
            return 0

    _write_lock(now)
    try:
        rc, output = _run_fetch_with_reauth()
        if rc != 0:
            msg = "WebUntis-Check fehlgeschlagen: Login/Datenabruf nicht möglich."
            tail = _tail(output)
            if tail:
                _append_log(tail)
            grace_minutes = _read_env_int(
                "WEBUNTIS_FAILURE_ALERT_GRACE_MINUTES",
                DEFAULT_FAILURE_ALERT_GRACE_MINUTES,
            )
            if _has_recent_success(state, now=now, grace_minutes=grace_minutes):
                note = (
                    "WebUntis-Einzelfehler unterdrückt: letzter erfolgreicher Abruf "
                    f"liegt weniger als {grace_minutes} Minuten zurück."
                )
                _append_log(note)
                print(note)
                return 0
            if _ntfy_ready():
                _send_school_alert(msg)
            print(msg)
            return rc

        if not LATEST_JSON.exists():
            msg = "WebUntis-Check fehlgeschlagen: latest_today.json fehlt."
            print(msg)
            return 2

        try:
            payload = json.loads(LATEST_JSON.read_text(encoding="utf-8"))
        except Exception:
            msg = "WebUntis-Check fehlgeschlagen: JSON konnte nicht gelesen werden."
            print(msg)
            return 2

        candidates = _extract_alert_candidates(payload, now=now, today=now.date())
        seen = _prune_seen(state.get("seen") if isinstance(state.get("seen"), dict) else {}, now=now)
        new_items = [item for item in candidates if item.key not in seen]
        duplicates = len(candidates) - len(new_items)

        notify_ok = True
        sent_count = 0
        if not _ntfy_ready():
            notify_ok = False
            _append_log("ntfy für WebUntis nicht konfiguriert.")
        else:
            for item in new_items:
                if _send_school_alert(item.display_text):
                    sent_count += 1
                    seen[item.key] = now.isoformat()
                else:
                    notify_ok = False
            for item in candidates:
                seen.setdefault(item.key, now.isoformat())

        calendar_rc, calendar_output = _run_python(CALENDAR_SYNC_SCRIPT)
        calendar_status = "ok"
        if "Rate Limit erreicht" in calendar_output or "Rate Limit" in calendar_output:
            calendar_status = "rate_limit"
        elif calendar_rc != 0:
            calendar_status = "failed"

        state_out = {"seen": seen, "last_run": now.isoformat(), "last_success_at": now.isoformat()}
        _save_state(STATE_FILE, state_out)

        summary = (
            f"WebUntis gelesen: ja | Änderungen: {len(candidates)} | Alerts: {len(candidates)} | "
            f"ntfy neu: {sent_count} | Duplikate: {duplicates} | ntfy: {'ok' if notify_ok else 'fehlgeschlagen'} | "
            f"Calendar: {calendar_status}"
        )
        _append_log(summary)

        if calendar_status == "rate_limit":
            print("WebUntis gelesen. ntfy geprüft. Calendar-Sync übersprungen: Google Rate Limit.")
        elif sent_count > 0 and calendar_status == "ok":
            print(f"WebUntis gelesen. {sent_count} ntfy-Meldung gesendet. Calendar-Sync ok.")
        elif len(candidates) == 0 and calendar_status == "ok":
            print("WebUntis gelesen. Keine neuen Entfälle. Calendar-Sync ok.")
        elif calendar_status == "failed":
            print("WebUntis gelesen. ntfy geprüft. Calendar-Sync fehlgeschlagen.")
        else:
            print("WebUntis gelesen. ntfy geprüft. Calendar-Sync ok.")
        return 0
    finally:
        _clear_lock()


if __name__ == "__main__":
    raise SystemExit(main())
