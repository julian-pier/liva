from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib import error as urllib_error
from urllib import request as urllib_request

from database.connections import get_core_db
from database.sqlite_backup import latest_backup_timestamp
from jobs.daily_backup import load_backup_metadata
from integrations.telegram_hub import ensure_telegram_schema, send_stateful_system_message


def _health_url() -> str:
    return (os.environ.get("LIVA_HEALTH_URL") or "http://127.0.0.1:5000/healthz").strip()


def _load_notice_row(notice_key: str) -> dict | None:
    ensure_telegram_schema()
    conn = get_core_db()
    conn.row_factory = None
    try:
        row = conn.execute(
            "SELECT status, last_sent_at, raw_json FROM telegram_notice_state WHERE notice_key=? LIMIT 1",
            (str(notice_key or "").strip(),),
        ).fetchone()
        if not row:
            return None
        raw_json = {}
        try:
            raw_json = json.loads(row[2]) if row[2] else {}
        except Exception:
            raw_json = {}
        return {
            "status": str(row[0] or ""),
            "last_sent_at": str(row[1] or ""),
            "raw_json": raw_json if isinstance(raw_json, dict) else {},
        }
    finally:
        conn.close()


def _store_notice_row(notice_key: str, status: str, raw_json: dict | None = None) -> None:
    ensure_telegram_schema()
    conn = get_core_db()
    try:
        conn.execute(
            """
            INSERT INTO telegram_notice_state (notice_key, status, last_sent_at, raw_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(notice_key) DO UPDATE SET
                status=excluded.status,
                last_sent_at=excluded.last_sent_at,
                raw_json=excluded.raw_json
            """,
            (
                str(notice_key or "").strip(),
                str(status or "").strip(),
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                json.dumps(raw_json or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _check_health() -> tuple[str, str]:
    url = _health_url()
    try:
        with urllib_request.urlopen(url, timeout=5) as response:
            status = int(getattr(response, "status", 200) or 200)
            body = response.read().decode("utf-8", errors="ignore").strip()
        body_json = None
        try:
            body_json = json.loads(body) if body else None
        except Exception:
            body_json = None
        if status == 200 and (
            body.lower() == "ok"
            or (isinstance(body_json, dict) and bool(body_json.get("ok")))
        ):
            return ("ok", "LIVA health passt.")
        return ("error", f"LIVA health failed ({status}).")
    except urllib_error.URLError:
        return ("error", "LIVA ist down oder nicht erreichbar.")
    except Exception as exc:
        return ("error", f"LIVA health check fehlgeschlagen: {exc}")


def _check_backup() -> tuple[str, str]:
    last_ts = latest_backup_timestamp()
    metadata = load_backup_metadata() or {}
    payload = metadata.get("payload") if isinstance(metadata, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    sqlite_backup = payload.get("sqlite_backup") if isinstance(payload.get("sqlite_backup"), dict) else {}
    remote_sync = sqlite_backup.get("remote_sync") if isinstance(sqlite_backup.get("remote_sync"), dict) else {}
    code_push = payload.get("code_push") if isinstance(payload.get("code_push"), dict) else {}

    if last_ts is None:
        return ("error", "Backups fehlen komplett.")

    fresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    last_dt = datetime.fromtimestamp(float(last_ts), tz=timezone.utc)
    if last_dt < fresh_cutoff:
        return ("error", f"Backup zu alt: zuletzt {last_dt.isoformat()}.")

    code_push_required = (os.getenv("LIVA_CODE_PUSH_REQUIRED") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if code_push_required and code_push and not bool(code_push.get("ok")):
        return ("error", f"Code-Backup fehlgeschlagen: {code_push.get('reason') or code_push.get('step') or 'Fehler'}.")
    if remote_sync and not bool(remote_sync.get("ok")):
        return ("error", f"Remote-Backup fehlgeschlagen: {remote_sync.get('reason') or remote_sync.get('status') or 'Fehler'}.")
    return ("ok", "Backups erfolgreich.")


def _check_disk() -> tuple[str, str]:
    try:
        import shutil

        usage = shutil.disk_usage("/var/lib/liva")
    except Exception:
        try:
            import shutil

            usage = shutil.disk_usage("/")
        except Exception as exc:
            return ("error", f"Speicherprüfung fehlgeschlagen: {exc}")
    total = float(usage.total or 0)
    free = float(usage.free or 0)
    used_pct = ((total - free) / total * 100.0) if total > 0 else 0.0
    free_pct = 100.0 - used_pct
    free_gb = free / (1024 ** 3)
    # Disk-Alarm nur noch bei sehr kritischem Restplatz.
    if free_pct <= 5.0:
        return ("error", f"Wenig Speicherplatz: nur noch {free_gb:.1f} GB frei ({free_pct:.0f}% frei).")
    return ("ok", f"Speicher ok: {free_gb:.1f} GB frei ({free_pct:.0f}% frei).")


def _sent_today(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    raw = str(row.get("last_sent_at") or "").strip()
    if not raw:
        return False
    try:
        last_dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return False
    return last_dt.astimezone(timezone.utc).date() == datetime.now(timezone.utc).date()


def main() -> int:
    health_status, health_message = _check_health()
    backup_status, backup_message = _check_backup()
    disk_status, disk_message = _check_disk()
    health_result = {"ok": True, "sent": False, "status": health_status}
    backup_result = {"ok": True, "sent": False, "status": backup_status}
    disk_result = {"ok": True, "sent": False, "status": disk_status}

    health_row = _load_notice_row("system:health_alarm") or {}
    health_meta = health_row.get("raw_json") if isinstance(health_row.get("raw_json"), dict) else {}
    now = datetime.now(timezone.utc)
    down_since_raw = str(health_meta.get("down_since") or "").strip()
    down_since = None
    if down_since_raw:
        try:
            down_since = datetime.fromisoformat(down_since_raw.replace("Z", "+00:00"))
        except Exception:
            down_since = None

    if health_status == "error":
        if down_since is None:
            down_since = now
        down_minutes = (now - down_since).total_seconds() / 60.0
        _store_notice_row(
            "system:health_alarm",
            health_row.get("status") or "pending",
            {"down_since": down_since.replace(microsecond=0).isoformat()},
        )
        if down_minutes >= 10.0:
            health_result = send_stateful_system_message(
                notice_key="system:health_alarm",
                status="error",
                message=f"{health_message} Seit mindestens 10 Minuten.",
                min_repeat_minutes=180,
            )
    else:
        if str(health_row.get("status") or "").strip() == "error":
            health_result = send_stateful_system_message(
                notice_key="system:health_alarm",
                status="ok",
                message="LIVA ist nach >10 Minuten Ausfall wieder online.",
                force=True,
            )
        _store_notice_row("system:health_alarm", "ok", {})

    if backup_status == "error":
        backup_result = send_stateful_system_message(
            notice_key="system:backup_alarm",
            status="error",
            message=backup_message,
            min_repeat_minutes=180,
        )
    else:
        _store_notice_row("system:backup_alarm", "ok", {})

    if disk_status == "error":
        disk_row = _load_notice_row("system:disk_alarm") or {}
        if _sent_today(disk_row):
            disk_result = {"ok": True, "sent": False, "status": "error"}
        else:
            disk_result = send_stateful_system_message(
                notice_key="system:disk_alarm",
                status="error",
                message=disk_message,
                min_repeat_minutes=1440,
            )
    else:
        _store_notice_row("system:disk_alarm", "ok", {})

    payload = {
        "ok": bool(health_result.get("ok")) and bool(backup_result.get("ok")) and bool(disk_result.get("ok")),
        "health": {"status": health_status, "message": health_message},
        "backup": {"status": backup_status, "message": backup_message},
        "disk": {"status": disk_status, "message": disk_message},
        "health_alarm": health_result,
        "backup_alarm": backup_result,
        "disk_alarm": disk_result,
    }
    print(json.dumps(payload, ensure_ascii=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
