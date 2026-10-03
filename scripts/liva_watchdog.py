#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import connections  # noqa: E402
from jobs.daily_backup import load_backup_metadata  # noqa: E402

WATCHDOG_DIR = Path("/var/lib/liva/.liva-watchdog")
WATCHDOG_DB = WATCHDOG_DIR / "watchdog.sqlite3"
ALERT_ONCE_BIN = Path("/var/lib/liva/bin/liva-alert-once")
BACKUP_SEARCH_PATHS = (
    Path("/var/lib/liva/backups"),
    Path("/opt/liva/backups"),
    Path("/opt/liva/database/backups"),
    Path("/opt/liva/backup"),
    Path("/var/lib/liva/backup"),
)
IGNORED_BACKUP_PATH_PARTS = {"bootstrap", "rescue", "manual", "old", "archive"}
ACCESS_LOG_PATTERNS = (
    "/var/log/nginx/access.log*",
    "/var/log/caddy/access.log*",
    "/opt/liva/logs/access.log*",
    "/opt/liva/logs/*.log",
)
IGNORED_BACKUP_SUFFIXES = {".tmp", ".part", ".lock"}
MAX_ACCESS_LOG_FILES = 6
MAX_ACCESS_LOG_LINES = 2000
MAX_GZ_LOG_BYTES = 2 * 1024 * 1024
JOURNAL_UNIT = "liva.service"
JOURNAL_SOURCE = "liva.service/journal"
JOURNAL_LOOKBACK = "24 hours ago"
SECURITY_ALERT_MIN_LEVEL = "critical"
SECURITY_ALERT_DEDUPE_HOURS = 6
SECURITY_LOOKBACK_HOURS = 24
SECURITY_BURST_WINDOW_MINUTES = 10
SECURITY_BURST_REQUEST_THRESHOLD = 20
SECURITY_UNIQUE_PATH_THRESHOLD = 8
SECURITY_4XX_THRESHOLD = 8
IGNORED_ACCESS_USER_AGENTS = ("uptime-kuma", "python-urllib")
CLIENT_IP_RE = re.compile(r"\bclient_ip=(?P<ip>[^\s,]+)")
X_FORWARDED_FOR_RE = re.compile(r"\bx_forwarded_for=(?P<ip>[^\s,]+)")
GUNICORN_PREFIX_RE = re.compile(r"^(?P<ip>\S+)\s+-\s+-\s+\[")
METHOD_RE = re.compile(r"\bmethod=(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\b", re.IGNORECASE)
PATH_RE = re.compile(r"\bpath=(?P<path>\S+)")
STATUS_RE = re.compile(r"\bstatus(?:_code)?=(?P<status>\d{3})\b", re.IGNORECASE)
USER_AGENT_RE = re.compile(r"\b(?:user_agent|ua)=(?P<ua>.+?)(?:\s+\w+=|$)", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"^(?P<ts>[A-Z][a-z]{2}\s+\d{2}\s+\d{2}:\d{2}:\d{2})")
GUNICORN_ACCESS_RE = re.compile(
    r'^(?P<ip>\S+)\s+-\s+-\s+\[(?P<ts>[^\]]+)\]\s+"(?P<method>[A-Z]+)\s+(?P<path>\S+)(?:\s+HTTP/[0-9.]+)?"\s+(?P<status>\d{3})'
)
LEVELS = ("ignore", "info", "low", "medium", "high", "critical")
LEVEL_RANK = {level: idx for idx, level in enumerate(LEVELS)}
HARMLESS_PATH_RE = re.compile(r"^/(?:|favicon\.ico|robots\.txt|manifest\.json|apple-touch-icon(?:-\d+x\d+)?(?:\.png)?)$")
SUSPICIOUS_PATH_PATTERNS = (
    "/.env", "/.git", "/.git/config", "/wp-admin", "/wp-login.php", "/xmlrpc.php",
    "/phpmyadmin", "/server-status", "/debug",
    "/actuator", "/console", "/api/.env", "/vendor/phpunit", "/cgi-bin",
)
EXPLOIT_STRINGS = ("../", "%2e%2e", "etc/passwd", "cmd=", "eval(", "base64", "${jndi:", "union select", "<script", "php://")
SENSITIVE_WRITE_PREFIXES = ("/api/v2/actions/", "/api/polar/", "/api/gcal/", "/api/settings/")
SENSITIVE_SCANNER_SUCCESS_PATHS = ("/.env", "/.git/config", "/backup", "/config")
KNOWN_BENIGN_READ_PATTERNS = (
    re.compile(r"^/api/settings(?:/|$)"),
    re.compile(r"^/api/hrv(?:/|$)"),
    re.compile(r"^/api/polar/(?:latest|status)(?:/|$)"),
    re.compile(r"^/api/v2/actions/(?:daily/context|training/coach-view)(?:/|$)"),
    re.compile(r"^/api/health(?:/|$)"),
    re.compile(r"^/health(?:/|$)"),
    re.compile(r"^/ping(?:/|$)"),
)
LOGIN_PATH_PREFIXES = ("/private_access", "/_private_access")


@dataclass(frozen=True)
class AccessEvent:
    ip: str
    method: str | None = None
    path: str | None = None
    status_code: int | None = None
    user_agent: str | None = None
    timestamp: datetime | None = None
    source: str = JOURNAL_SOURCE
    raw_line: str = ""


@dataclass
class IpActivity:
    ip: str
    source: str
    total_requests: int = 0
    unique_paths: set[str] = field(default_factory=set)
    methods_count: dict[str, int] = field(default_factory=dict)
    status_count: dict[str, int] = field(default_factory=dict)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    suspicious_paths: set[str] = field(default_factory=set)
    auth_failures: int = 0
    write_attempts: int = 0
    scan_score: int = 0
    events: list[AccessEvent] = field(default_factory=list)
    exploit_hits: set[str] = field(default_factory=set)
    burst_requests: int = 0
    top_paths: list[str] = field(default_factory=list)
    redirect_login_count: int = 0
    benign_read_requests: int = 0


@dataclass
class SecurityDebugStats:
    external_ips_seen: int = 0
    ignored_events: int = 0
    levels: dict[str, int] = field(default_factory=lambda: {level: 0 for level in LEVELS})
    alerts_sent: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CheckResult:
    check_key: str
    status: str
    message: str
    detail: str | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_state_db(db_path: Path = WATCHDOG_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS check_states (
            check_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            message_hash TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_ips (
            ip TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            source TEXT,
            last_alerted_at TEXT,
            highest_level_alerted TEXT,
            recent_event_signature TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_ips)").fetchall()}
    migrations = {
        "last_alerted_at": "ALTER TABLE seen_ips ADD COLUMN last_alerted_at TEXT",
        "highest_level_alerted": "ALTER TABLE seen_ips ADD COLUMN highest_level_alerted TEXT",
        "recent_event_signature": "ALTER TABLE seen_ips ADD COLUMN recent_event_signature TEXT",
    }
    for column, sql in migrations.items():
        if column not in columns:
            conn.execute(sql)
    conn.commit()
    return conn


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def has_value(value: Any) -> bool:
    return value is not None and value != ""


def isoformat_seconds(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def age_hours_text(past: datetime, *, now: datetime | None = None) -> str:
    current = now or utc_now()
    hours = max(0.0, (current - past).total_seconds() / 3600.0)
    return f"{hours:.1f}"


def is_complete_recovery_row(row: dict[str, Any]) -> bool:
    rmssd = coalesce(row.get("rmssd"), row.get("mean_recovery_rmssd"))
    rri_or_pulse = coalesce(
        row.get("rri"),
        row.get("mean_recovery_rri"),
        row.get("nightPulse"),
        row.get("sleepPulse"),
        row.get("night_pulse"),
        row.get("sleep_pulse"),
    )
    sleep_score = coalesce(row.get("sleepScore"), row.get("sleep_score"))
    return has_value(rmssd) and has_value(rri_or_pulse) and has_value(sleep_score)


def fetch_recovery_rows_from_endpoint(
    *,
    url: str = "http://127.0.0.1:5000/api/hrv/recovery?range_days=14&baseline_days=28",
    timeout_seconds: float = 5.0,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        with urllib_request.urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib_error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None, "endpoint_unreadable"
    except Exception:
        return None, "endpoint_unreadable"
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return None, "endpoint_unreadable"
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None, "endpoint_unreadable"
    out = [row for row in rows if isinstance(row, dict)]
    return out, None


def fetch_recovery_rows_from_database(db_path: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    polar_db = db_path or connections.POLAR_DB
    if not polar_db or not Path(polar_db).exists():
        return [], False
    conn = sqlite3.connect(str(polar_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH dates AS (
                SELECT date FROM polar_nightly_recharge
                UNION
                SELECT date FROM polar_sleep
            )
            SELECT
                d.date,
                n.mean_recovery_rmssd,
                n.mean_recovery_rri,
                s.sleep_score,
                n.updated_at AS nightly_updated_at,
                s.updated_at AS sleep_updated_at
            FROM dates d
            LEFT JOIN polar_nightly_recharge n ON n.date = d.date
            LEFT JOIN polar_sleep s ON s.date = d.date
            ORDER BY d.date DESC
            """
        ).fetchall()
    finally:
        conn.close()
    out = []
    has_any_data = False
    for raw in rows:
        row = dict(raw)
        if any(has_value(row.get(key)) for key in ("mean_recovery_rmssd", "mean_recovery_rri", "sleep_score")):
            has_any_data = True
        out.append(
            {
                "date": row.get("date"),
                "time": coalesce(row.get("sleep_updated_at"), row.get("nightly_updated_at"), row.get("date")),
                "mean_recovery_rmssd": row.get("mean_recovery_rmssd"),
                "mean_recovery_rri": row.get("mean_recovery_rri"),
                "sleep_score": row.get("sleep_score"),
            }
        )
    return out, has_any_data


def evaluate_recovery(*, now: datetime | None = None) -> CheckResult:
    current = now or utc_now()
    rows, endpoint_error = fetch_recovery_rows_from_endpoint()
    fallback_has_any_data = False
    if rows is None:
        db_rows, fallback_has_any_data = fetch_recovery_rows_from_database()
        if db_rows:
            rows = db_rows
        elif endpoint_error:
            return CheckResult("recovery", "alert", "Recovery-Endpoint nicht lesbar")
    assert rows is not None
    complete_rows = [row for row in rows if is_complete_recovery_row(row)]
    if not complete_rows:
        any_data = fallback_has_any_data or any(any(has_value(value) for value in row.values()) for row in rows)
        if any_data:
            return CheckResult("recovery", "alert", "Keine vollständige HRV/RHR/Sleep-Nacht gefunden")
        return CheckResult("recovery", "alert", "Recovery-Endpoint nicht lesbar" if endpoint_error else "Keine vollständige HRV/RHR/Sleep-Nacht gefunden")
    latest = complete_rows[0]
    latest_dt = parse_timestamp(coalesce(latest.get("time"), latest.get("date")))
    if latest_dt is None and latest.get("date"):
        latest_dt = parse_timestamp(f"{latest['date']}T00:00:00+00:00")
    if latest_dt is None:
        return CheckResult("recovery", "alert", "Keine vollständige HRV/RHR/Sleep-Nacht gefunden")
    stale_cutoff = current - timedelta(hours=36)
    if latest_dt < stale_cutoff:
        return CheckResult(
            "recovery",
            "alert",
            f"LIVA: HRV/RHR/Sleep-Daten sind veraltet (letzte vollständige Nacht: {isoformat_seconds(latest_dt)}, {age_hours_text(latest_dt, now=current)}h alt).",
            detail=f"stale:{isoformat_seconds(latest_dt)}",
        )
    return CheckResult(
        "recovery",
        "ok",
        f"Recovery frisch (letzte vollständige Nacht: {isoformat_seconds(latest_dt)}, {age_hours_text(latest_dt, now=current)}h alt).",
        detail=f"fresh:{isoformat_seconds(latest_dt)}",
    )


def iter_backup_candidates(paths: Sequence[Path], *, max_files: int = 5000) -> Iterable[Path]:
    seen = 0
    for base in paths:
        if not base.exists() or not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [name for name in dirs if not name.startswith(".cache")]
            for name in files:
                seen += 1
                if seen > max_files:
                    return
                path = Path(root) / name
                yield path


def is_temp_backup_file(path: Path) -> bool:
    suffixes = {suffix.lower() for suffix in path.suffixes}
    return any(suffix in IGNORED_BACKUP_SUFFIXES for suffix in suffixes)


def is_ignored_backup_path(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    return any(token in parts for token in IGNORED_BACKUP_PATH_PARTS) or any(token in name for token in IGNORED_BACKUP_PATH_PARTS)


def find_backup_files(paths: Sequence[Path] | None = None) -> tuple[list[Path], list[Path], list[Path]]:
    active_paths = tuple(paths or BACKUP_SEARCH_PATHS)
    existing_dirs = [path for path in active_paths if path.exists() and path.is_dir()]
    valid: list[Path] = []
    invalid_zero: list[Path] = []
    temp_only: list[Path] = []
    for path in iter_backup_candidates(existing_dirs):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if is_temp_backup_file(path):
            temp_only.append(path)
            continue
        if is_ignored_backup_path(path):
            continue
        if stat.st_size > 0:
            valid.append(path)
        else:
            invalid_zero.append(path)
    return valid, invalid_zero, temp_only


def get_failed_backup_units() -> list[str]:
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--failed", "--type=service", "--plain", "--no-legend", "--no-pager"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    units = []
    for line in result.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        if "backup" in unit.lower():
            units.append(unit)
    return units


def _backup_metadata_payload() -> dict[str, Any]:
    metadata = load_backup_metadata() or {}
    payload = metadata.get("payload") if isinstance(metadata, dict) else {}
    return payload if isinstance(payload, dict) else {}


def evaluate_backup(*, now: datetime | None = None) -> CheckResult:
    current = now or utc_now()
    payload = _backup_metadata_payload()
    sqlite_backup = payload.get("sqlite_backup") if isinstance(payload.get("sqlite_backup"), dict) else {}
    remote_sync = sqlite_backup.get("remote_sync") if isinstance(sqlite_backup.get("remote_sync"), dict) else {}
    failed_units = get_failed_backup_units()
    if failed_units and not (bool(sqlite_backup.get("ok")) and bool(remote_sync.get("ok", True))):
        return CheckResult("backup", "alert", f"LIVA: Backup fehlgeschlagen ({failed_units[0]} ist failed).")
    valid_files, invalid_zero, temp_only = find_backup_files()
    existing_dirs = [path for path in BACKUP_SEARCH_PATHS if path.exists() and path.is_dir()]
    if not valid_files and not invalid_zero and not temp_only:
        return CheckResult("backup", "skip", "backup check skipped: no backup evidence found")
    if invalid_zero and not valid_files:
        return CheckResult("backup", "alert", f"LIVA: Backup-Datei ist leer oder ungültig: {invalid_zero[0]}")
    if temp_only and not valid_files and not invalid_zero:
        return CheckResult("backup", "alert", f"LIVA: Backup-Datei ist leer oder ungültig: {temp_only[0]}")
    if not valid_files:
        return CheckResult("backup", "skip", "Backup-Dateien nicht eindeutig beurteilbar")
    latest = max(valid_files, key=lambda path: path.stat().st_mtime)
    latest_dt = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    if latest_dt < current - timedelta(hours=48):
        return CheckResult(
            "backup",
            "alert",
            f"LIVA: Backup veraltet (letztes Backup: {latest}, {age_hours_text(latest_dt, now=current)}h alt).",
        )
    return CheckResult("backup", "ok", f"Backup frisch ({latest}).")


def get_mount_usage(path: str) -> tuple[float, str] | None:
    try:
        usage = shutil.disk_usage(path)
    except Exception:
        return None
    if not usage.total:
        return None
    used_pct = ((usage.total - usage.free) / usage.total) * 100.0
    return used_pct, path


def read_proc_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            value = rest.strip().split()[0]
            if value.isdigit():
                values[key] = int(value)
    return values


def get_ram_used_pct() -> float | None:
    try:
        meminfo = read_proc_meminfo()
    except Exception:
        return None
    total = float(meminfo.get("MemTotal") or 0)
    available = float(meminfo.get("MemAvailable") or 0)
    if total <= 0:
        return None
    return ((total - available) / total) * 100.0


def get_load15() -> float | None:
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[2])
    except Exception:
        return None


def evaluate_system() -> CheckResult:
    disk_issues: list[tuple[str, str]] = []
    root_usage = get_mount_usage("/")
    if root_usage:
        root_pct, _ = root_usage
        if root_pct > 95:
            disk_issues.append(("alert", f"LIVA: Speicherplatz kritisch (/ ist {root_pct:.0f}% voll)."))
        elif root_pct > 85:
            disk_issues.append(("alert", f"LIVA: Speicherplatz knapp (/ ist {root_pct:.0f}% voll)."))
    try:
        root_dev = os.stat("/").st_dev
        home_dev = os.stat("/home").st_dev
    except Exception:
        root_dev = home_dev = None
    if home_dev is not None and root_dev is not None and home_dev != root_dev:
        home_usage = get_mount_usage("/home")
        if home_usage:
            home_pct, _ = home_usage
            if home_pct > 95:
                disk_issues.append(("alert", f"LIVA: Speicherplatz kritisch (/home ist {home_pct:.0f}% voll)."))
            elif home_pct > 85:
                disk_issues.append(("alert", f"LIVA: Speicherplatz knapp (/home ist {home_pct:.0f}% voll)."))
    ram_pct = get_ram_used_pct()
    if ram_pct is not None and ram_pct > 90:
        return CheckResult("system", "alert", f"LIVA: RAM hoch ({ram_pct:.0f}% belegt).")
    load15 = get_load15()
    cores = os.cpu_count() or 1
    if load15 is not None and load15 > cores * 4:
        return CheckResult("system", "alert", f"LIVA: Systemlast hoch (Load15 {load15:.1f} bei {cores} Kernen).")
    if load15 is not None and load15 > cores * 2:
        return CheckResult("system", "alert", f"LIVA: Systemlast hoch (Load15 {load15:.1f} bei {cores} Kernen).")
    if disk_issues:
        severity, message = sorted(disk_issues, key=lambda item: len(item[1]), reverse=True)[0]
        return CheckResult("system", severity, message)
    return CheckResult("system", "ok", "Systemlast im Rahmen.")


def iter_access_log_paths() -> list[Path]:
    import glob

    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in ACCESS_LOG_PATTERNS:
        for raw in glob.glob(pattern):
            path = Path(raw)
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            out.append(path)
    return sorted(out, key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)[:MAX_ACCESS_LOG_FILES]


def open_text_log(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def recent_log_lines(path: Path, *, max_lines: int = MAX_ACCESS_LOG_LINES) -> list[str]:
    try:
        if path.suffix == ".gz" and path.stat().st_size > MAX_GZ_LOG_BYTES:
            return []
        with open_text_log(path) as handle:
            return list(deque(handle, maxlen=max_lines))
    except OSError:
        return []


def normalize_ip_text(value: str) -> str | None:
    cleaned = value.strip().strip('"').strip("'").strip(",")
    if cleaned.lower() == "localhost":
        return None
    if cleaned.startswith("::ffff:"):
        cleaned = cleaned[7:]
    return cleaned or None


def parse_ip(value: str) -> ipaddress._BaseAddress | None:
    normalized = normalize_ip_text(value)
    if not normalized:
        return None
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        return None


def is_ignored_client_ip(value: str) -> bool:
    ip = parse_ip(value)
    if ip is None:
        return True
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and (
        ip in ipaddress.ip_network("10.0.0.0/8")
        or ip in ipaddress.ip_network("172.16.0.0/12")
        or ip in ipaddress.ip_network("192.168.0.0/16")
    ):
        return True
    return False


def should_ignore_access_line(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in IGNORED_ACCESS_USER_AGENTS)


def parse_loose_timestamp(value: str | None, *, year: int | None = None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    if year is not None:
        try:
            return datetime.strptime(f"{year} {text}", "%Y %b %d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def extract_ips_from_line(line: str) -> list[str]:
    if should_ignore_access_line(line):
        return []
    found: list[str] = []
    for token in line.replace("[", " ").replace("]", " ").replace(",", " ").split():
        normalized = normalize_ip_text(token)
        if not normalized or is_ignored_client_ip(normalized):
            continue
        ip = parse_ip(normalized)
        if ip is None:
            continue
        found.append(str(ip))
    return found


def extract_access_ip_from_journal_line(line: str) -> str | None:
    if should_ignore_access_line(line):
        return None
    match = CLIENT_IP_RE.search(line)
    if match:
        candidate = normalize_ip_text(match.group("ip") or "")
        if candidate and not is_ignored_client_ip(candidate):
            return candidate
    match = GUNICORN_PREFIX_RE.search(line.strip())
    if match:
        candidate = normalize_ip_text(match.group("ip") or "")
        if candidate and not is_ignored_client_ip(candidate):
            return candidate
    match = X_FORWARDED_FOR_RE.search(line)
    if match:
        raw = match.group("ip") or ""
        first = raw.split(",")[0].strip()
        candidate = normalize_ip_text(first)
        if candidate and not is_ignored_client_ip(candidate):
            return candidate
    return None


def parse_access_event_from_line(line: str, *, source: str = JOURNAL_SOURCE, now: datetime | None = None) -> AccessEvent | None:
    if should_ignore_access_line(line):
        return None
    stripped = line.strip()
    gunicorn = GUNICORN_ACCESS_RE.search(stripped)
    if gunicorn:
        ip = normalize_ip_text(gunicorn.group("ip") or "")
        if not ip or is_ignored_client_ip(ip):
            return None
        return AccessEvent(
            ip=ip,
            method=(gunicorn.group("method") or "").upper() or None,
            path=gunicorn.group("path"),
            status_code=int(gunicorn.group("status")),
            timestamp=parse_loose_timestamp(gunicorn.group("ts")),
            source=source,
            raw_line=line,
        )
    ip = extract_access_ip_from_journal_line(line)
    if not ip:
        return None
    method_match = METHOD_RE.search(line)
    path_match = PATH_RE.search(line)
    status_match = STATUS_RE.search(line)
    ua_match = USER_AGENT_RE.search(line)
    ts_match = TIMESTAMP_RE.search(line)
    current_year = (now or utc_now()).year
    return AccessEvent(
        ip=ip,
        method=(method_match.group("method").upper() if method_match else None),
        path=path_match.group("path") if path_match else None,
        status_code=int(status_match.group("status")) if status_match else None,
        user_agent=ua_match.group("ua").strip().strip('"') if ua_match else None,
        timestamp=parse_loose_timestamp(ts_match.group("ts") if ts_match else None, year=current_year),
        source=source,
        raw_line=line,
    )


def normalize_path_for_analysis(path: str | None) -> str | None:
    if not path:
        return None
    text = path.strip()
    if not text:
        return None
    parsed = urlsplit(text)
    clean_path = parsed.path or text
    if clean_path.startswith(LOGIN_PATH_PREFIXES):
        next_values = parse_qs(parsed.query).get("next", [])
        if next_values:
            nested = normalize_path_for_analysis(next_values[0])
            if nested:
                return nested
    return clean_path or None


def is_login_path(path: str | None) -> bool:
    if not path:
        return False
    return path.startswith(LOGIN_PATH_PREFIXES)


def is_benign_read_path(path: str | None) -> bool:
    if not path:
        return False
    normalized = normalize_path_for_analysis(path)
    if not normalized:
        return False
    return any(pattern.match(normalized) for pattern in KNOWN_BENIGN_READ_PATTERNS)


def read_liva_journal_lines(
    *,
    unit: str = JOURNAL_UNIT,
    since: str = JOURNAL_LOOKBACK,
    timeout_seconds: float = 5.0,
) -> list[str]:
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "--since", since, "--no-pager"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def scan_access_events(*, now: datetime | None = None) -> list[AccessEvent]:
    found: list[AccessEvent] = []
    for line in read_liva_journal_lines():
        event = parse_access_event_from_line(line, source=JOURNAL_SOURCE, now=now)
        if event:
            found.append(event)
    for path in iter_access_log_paths():
        for line in recent_log_lines(path):
            event = parse_access_event_from_line(line, source=str(path), now=now)
            if event:
                found.append(event)
    return found


def scan_access_ips() -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for event in scan_access_events():
        item = (event.ip, event.source)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def is_suspicious_path(path: str) -> bool:
    normalized = normalize_path_for_analysis(path)
    if not normalized:
        return False
    lowered = normalized.lower()
    if is_benign_read_path(normalized):
        return False
    if lowered in {"/config", "/backup"} or lowered.startswith("/config/") or lowered.startswith("/backup/"):
        return True
    return any(token in lowered for token in SUSPICIOUS_PATH_PATTERNS) or any(token in lowered for token in EXPLOIT_STRINGS)


def is_sensitive_write_path(path: str | None) -> bool:
    normalized = normalize_path_for_analysis(path)
    if not normalized:
        return False
    return normalized.startswith(SENSITIVE_WRITE_PREFIXES)


def is_sensitive_scanner_success_path(path: str | None) -> bool:
    normalized = normalize_path_for_analysis(path)
    if not normalized:
        return False
    lowered = normalized.lower()
    return (
        lowered in SENSITIVE_SCANNER_SUCCESS_PATHS
        or lowered.startswith("/backup/")
        or lowered.startswith("/config/")
    )


def aggregate_ip_activities(events: Sequence[AccessEvent], *, now: datetime | None = None) -> dict[str, IpActivity]:
    current = now or utc_now()
    burst_cutoff = current - timedelta(minutes=SECURITY_BURST_WINDOW_MINUTES)
    activities: dict[str, IpActivity] = {}
    for event in events:
        activity = activities.setdefault(event.ip, IpActivity(ip=event.ip, source=event.source))
        activity.total_requests += 1
        activity.events.append(event)
        activity.source = event.source
        normalized_path = normalize_path_for_analysis(event.path)
        if normalized_path:
            activity.unique_paths.add(normalized_path)
            if is_suspicious_path(normalized_path):
                activity.suspicious_paths.add(normalized_path)
            lowered = normalized_path.lower()
            hits = [token for token in EXPLOIT_STRINGS if token in lowered]
            activity.exploit_hits.update(hits)
            if is_login_path(event.path) and event.status_code == 302:
                activity.redirect_login_count += 1
            if event.method == "GET" and is_benign_read_path(normalized_path):
                activity.benign_read_requests += 1
        if event.method:
            activity.methods_count[event.method] = activity.methods_count.get(event.method, 0) + 1
            if event.method in {"POST", "PUT", "PATCH", "DELETE"}:
                activity.write_attempts += 1
        if event.status_code is not None:
            key = str(event.status_code)
            activity.status_count[key] = activity.status_count.get(key, 0) + 1
            if event.status_code in {401, 403}:
                activity.auth_failures += 1
        if event.timestamp:
            if activity.first_seen is None or event.timestamp < activity.first_seen:
                activity.first_seen = event.timestamp
            if activity.last_seen is None or event.timestamp > activity.last_seen:
                activity.last_seen = event.timestamp
            if event.timestamp >= burst_cutoff:
                activity.burst_requests += 1
        else:
            activity.last_seen = activity.last_seen or current
            activity.first_seen = activity.first_seen or current
            activity.burst_requests += 1
    for activity in activities.values():
        path_counts: dict[str, int] = {}
        for event in activity.events:
            normalized_path = normalize_path_for_analysis(event.path)
            if normalized_path:
                path_counts[normalized_path] = path_counts.get(normalized_path, 0) + 1
        ranked_paths = sorted(path_counts.items(), key=lambda item: (-item[1], item[0]))
        filtered_paths = [path for path, _ in ranked_paths if not is_login_path(path)]
        activity.top_paths = (filtered_paths or [path for path, _ in ranked_paths])[:5]
        activity.scan_score = (
            len(activity.suspicious_paths) * 3
            + len(activity.exploit_hits) * 4
            + activity.auth_failures * 2
            + activity.write_attempts * 2
            + max(0, len(activity.unique_paths) - 3)
            + sum(count for status, count in activity.status_count.items() if status.startswith("4"))
            + max(0, activity.redirect_login_count - 2)
        )
    return activities


def classify_security_event(activity: IpActivity) -> dict[str, Any]:
    reasons: list[str] = []
    suspicious = sorted(activity.suspicious_paths)
    four_xx = sum(count for status, count in activity.status_count.items() if status.startswith("4"))
    three_xx = sum(count for status, count in activity.status_count.items() if status.startswith("3"))
    two_xx = sum(count for status, count in activity.status_count.items() if status.startswith("2"))
    harmless_only = bool(activity.unique_paths) and all(HARMLESS_PATH_RE.match(path or "") for path in activity.unique_paths)
    high_signal_count = 0
    benign_read_only = activity.total_requests > 0 and activity.benign_read_requests == activity.total_requests and activity.write_attempts == 0

    if suspicious:
        high_signal_count += 1
        reasons.append("sensible Pfade angefragt")
    if activity.exploit_hits:
        high_signal_count += 1
        reasons.append("Exploit-Muster im Pfad")
    if four_xx >= SECURITY_4XX_THRESHOLD:
        high_signal_count += 1
        reasons.append("viele 4xx-Antworten")
    if len(activity.unique_paths) >= SECURITY_UNIQUE_PATH_THRESHOLD:
        reasons.append("viele unbekannte Pfade")
    if activity.burst_requests >= SECURITY_BURST_REQUEST_THRESHOLD:
        reasons.append("hohe Request-Rate")
    if activity.redirect_login_count >= 3:
        reasons.append("wiederholte Login-Redirects")
    unusual_methods = sorted(method for method in activity.methods_count if method in {"OPTIONS", "TRACE", "PUT", "DELETE"})
    if unusual_methods:
        reasons.append("ungewöhnliche Methoden")

    sensitive_write = any(event.method in {"POST", "PUT", "PATCH", "DELETE"} and is_sensitive_write_path(event.path) for event in activity.events)
    successful_sensitive_scan = any(
        (event.status_code or 0) >= 200
        and (event.status_code or 0) < 300
        and is_sensitive_scanner_success_path(event.path)
        for event in activity.events
    )
    protected_area_auth_failures = sum(
        1
        for event in activity.events
        if (event.status_code in {401, 403}) and (is_sensitive_write_path(event.path) or is_suspicious_path(event.path or ""))
    )
    if sensitive_write:
        high_signal_count += 1
        reasons.append("Write-Zugriff auf sensible API")
    if activity.auth_failures >= 3:
        high_signal_count += 1
        reasons.append("mehrere Auth-Fehlschläge")
    if successful_sensitive_scan:
        high_signal_count += 1
        reasons.append("sensible Datei erfolgreich erreichbar")

    if harmless_only and activity.total_requests <= 3 and four_xx == 0 and not suspicious and activity.write_attempts == 0:
        level = "ignore"
        if not reasons:
            reasons.append("harmlose Einzelzugriffe")
    elif benign_read_only and activity.auth_failures == 0 and not activity.exploit_hits and activity.redirect_login_count < 3:
        if activity.total_requests <= 3:
            level = "info"
        elif activity.burst_requests >= SECURITY_BURST_REQUEST_THRESHOLD or unusual_methods:
            level = "medium"
        else:
            level = "low"
        if not reasons:
            reasons.append("legitime Read-Endpunkte")
    elif successful_sensitive_scan:
        level = "critical"
    elif sensitive_write and (
        activity.auth_failures >= 1
        or activity.burst_requests >= SECURITY_BURST_REQUEST_THRESHOLD
        or protected_area_auth_failures >= 1
        or high_signal_count >= 1
    ):
        level = "critical"
    elif protected_area_auth_failures >= 3:
        level = "critical"
    elif activity.auth_failures >= 6:
        level = "critical"
    elif sensitive_write and activity.write_attempts >= 1:
        level = "critical"
    elif activity.burst_requests >= SECURITY_BURST_REQUEST_THRESHOLD and (sensitive_write or activity.auth_failures >= 1):
        level = "critical"
    elif suspicious or activity.exploit_hits or (activity.write_attempts and activity.auth_failures) or four_xx >= SECURITY_4XX_THRESHOLD:
        level = "high"
    elif (
        len(activity.unique_paths) >= SECURITY_UNIQUE_PATH_THRESHOLD
        or unusual_methods
        or four_xx >= 4
        or activity.scan_score >= 8
        or activity.redirect_login_count >= 3
        or (three_xx >= 5 and len(activity.unique_paths) >= 5)
    ):
        level = "medium"
    elif activity.total_requests >= 2 or activity.write_attempts:
        level = "low"
    else:
        level = "info"

    summary = reasons[0] if reasons else "neue Aktivitaet"
    return {
        "level": level,
        "reasons": reasons,
        "summary": summary,
        "top_paths": suspicious[:5] or activity.top_paths[:5],
        "four_xx": four_xx,
    }


def learn_access_ips(conn: sqlite3.Connection, entries: Sequence[tuple[str, str]], *, now: datetime | None = None) -> int:
    current = isoformat_seconds(now or utc_now())
    learned = 0
    for ip, source in entries:
        row = conn.execute("SELECT ip FROM seen_ips WHERE ip = ?", (ip,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO seen_ips (ip, first_seen, last_seen, source) VALUES (?, ?, ?, ?)",
                (ip, current, current, source),
            )
            learned += 1
        else:
            conn.execute(
                "UPDATE seen_ips SET last_seen = ?, source = ? WHERE ip = ?",
                (current, source, ip),
            )
    conn.commit()
    return learned


def _event_signature(ip: str, level: str, reasons: Sequence[str], top_paths: Sequence[str]) -> str:
    payload = json.dumps({"ip": ip, "level": level, "reasons": list(reasons)[:3], "top_paths": list(top_paths)[:5]}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def format_security_alert(ip: str, activity: IpActivity, classification: dict[str, Any]) -> str:
    level = classification["level"]
    emoji = {"medium": "🟠", "high": "🔴", "critical": "🚨"}.get(level, "ℹ️")
    title = {"medium": "Scan-Verdacht", "high": "Verdächtiger Zugriff", "critical": "Kritischer Sicherheitszugriff"}.get(level, "Security Event")
    methods = "/".join(sorted(method for method in activity.methods_count if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})) or "unbekannt"
    top_paths = ", ".join(classification["top_paths"][:3]) or "-"
    return (
        f"{emoji} LIVA Security: {title}\n"
        f"IP: {ip}\n"
        f"Risiko: {level}\n"
        f"Requests: {activity.total_requests} · 404: {classification['four_xx']} · Pfade: {len(activity.unique_paths)}\n"
        f"Methode: {methods}\n"
        f"Grund: {classification['summary']}\n"
        f"Top-Pfade: {top_paths}"
    )


def should_send_security_alert(conn: sqlite3.Connection, ip: str, classification: dict[str, Any], *, now: datetime | None = None) -> bool:
    level = classification["level"]
    if LEVEL_RANK[level] < LEVEL_RANK[SECURITY_ALERT_MIN_LEVEL]:
        return False
    row = conn.execute(
        "SELECT last_alerted_at, highest_level_alerted, recent_event_signature FROM seen_ips WHERE ip = ?",
        (ip,),
    ).fetchone()
    current = now or utc_now()
    signature = _event_signature(ip, level, classification["reasons"], classification["top_paths"])
    if row is None:
        return True
    highest = row["highest_level_alerted"] or "ignore"
    if LEVEL_RANK[level] > LEVEL_RANK.get(highest, 0):
        return True
    last_alerted_at = parse_timestamp(row["last_alerted_at"])
    if row["recent_event_signature"] == signature and last_alerted_at and last_alerted_at >= current - timedelta(hours=SECURITY_ALERT_DEDUPE_HOURS):
        return False
    return True


def update_security_alert_state(conn: sqlite3.Connection, ip: str, classification: dict[str, Any], *, now: datetime | None = None) -> None:
    current = isoformat_seconds(now or utc_now())
    signature = _event_signature(ip, classification["level"], classification["reasons"], classification["top_paths"])
    conn.execute(
        """
        UPDATE seen_ips
        SET last_alerted_at = ?, highest_level_alerted = ?, recent_event_signature = ?
        WHERE ip = ?
        """,
        (current, classification["level"], signature, ip),
    )
    conn.commit()


def evaluate_access(
    conn: sqlite3.Connection,
    *,
    learn_only: bool = False,
    now: datetime | None = None,
    debug: bool = False,
) -> CheckResult:
    current = now or utc_now()
    events = scan_access_events(now=current)
    if not events:
        return CheckResult("access", "skip", "Access-Logs nicht lesbar oder keine externen IPs gefunden")
    unique_entries = sorted({(event.ip, event.source) for event in events})
    existing_count = conn.execute("SELECT COUNT(*) FROM seen_ips").fetchone()[0]
    if learn_only or existing_count == 0:
        learned = learn_access_ips(conn, unique_entries, now=current)
        return CheckResult("access", "ok", f"Access-IPs gelernt ({learned} neue Einträge).")

    stats = SecurityDebugStats()
    stats.external_ips_seen = len({event.ip for event in events})
    activities = aggregate_ip_activities(events, now=current)
    candidates: list[tuple[str, IpActivity, dict[str, Any], str]] = []
    iso_now = isoformat_seconds(current)
    for ip, activity in activities.items():
        row = conn.execute("SELECT ip FROM seen_ips WHERE ip = ?", (ip,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO seen_ips (ip, first_seen, last_seen, source) VALUES (?, ?, ?, ?)",
                (ip, iso_now, iso_now, activity.source),
            )
        else:
            conn.execute("UPDATE seen_ips SET last_seen = ?, source = ? WHERE ip = ?", (iso_now, activity.source, ip))
        classification = classify_security_event(activity)
        stats.levels[classification["level"]] += 1
        if classification["level"] in {"ignore", "low", "info"}:
            stats.ignored_events += 1
        if should_send_security_alert(conn, ip, classification, now=current):
            candidates.append((ip, activity, classification, format_security_alert(ip, activity, classification)))
    conn.commit()
    if debug:
        print(
            "SECURITY DEBUG: "
            f"ips={stats.external_ips_seen} ignored={stats.ignored_events} "
            + " ".join(f"{level}={stats.levels[level]}" for level in LEVELS)
        )
    if not candidates:
        return CheckResult("access", "ok", "Keine sicherheitsrelevanten Access-Muster erkannt.")
    ip, activity, classification, message = max(candidates, key=lambda item: (LEVEL_RANK[item[2]["level"]], item[1].scan_score, item[1].total_requests))
    update_security_alert_state(conn, ip, classification, now=current)
    return CheckResult("access", "alert", message, detail=json.dumps({"ip": ip, "level": classification["level"]}))


def load_check_state(conn: sqlite3.Connection, check_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT check_key, status, message_hash, updated_at FROM check_states WHERE check_key = ?", (check_key,)).fetchone()


def _result_fingerprint(result: CheckResult) -> str:
    """Deduplicate an open condition, not volatile telemetry in its wording."""
    return f"{result.check_key}:{result.status}"


def store_check_state(conn: sqlite3.Connection, result: CheckResult) -> None:
    msg_hash = hashlib.sha256(_result_fingerprint(result).encode("utf-8")).hexdigest()
    conn.execute(
        """
        INSERT INTO check_states (check_key, status, message_hash, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(check_key) DO UPDATE SET
            status = excluded.status,
            message_hash = excluded.message_hash,
            updated_at = excluded.updated_at
        """,
        (result.check_key, result.status, msg_hash, isoformat_seconds(utc_now())),
    )
    conn.commit()


def clear_check_state(conn: sqlite3.Connection, check_key: str) -> None:
    conn.execute("DELETE FROM check_states WHERE check_key = ?", (check_key,))
    conn.commit()


def should_send_alert(conn: sqlite3.Connection, result: CheckResult) -> bool:
    if result.status != "alert":
        if result.status == "ok":
            clear_check_state(conn, result.check_key)
        else:
            store_check_state(conn, result)
        return False
    state = load_check_state(conn, result.check_key)
    msg_hash = hashlib.sha256(_result_fingerprint(result).encode("utf-8")).hexdigest()
    # A threshold remains one open episode even while free-space percentages or
    # data-age wording change on every timer tick.  A recovery clears this
    # state, so a later regression still produces a new notification.
    if state and state["status"] == "alert":
        return False
    store_check_state(conn, result)
    return True


def send_alert(check_key: str, message: str, *, dry_run: bool = False) -> None:
    if dry_run:
        return
    # Technical watchdog alerts share the canonical provider, audit and
    # dedupe path.  They must never bypass it through liva-notify/Ntfy.
    from control_center import notifications
    provider = notifications.status()["active_provider"]
    notifications.deliver(
        provider=provider,
        dedupe_key=check_key,
        text=message,
    )


def run_checks(
    check_names: Sequence[str],
    *,
    dry_run: bool = False,
    learn_access_only: bool = False,
    security_debug: bool = False,
    now: datetime | None = None,
    db_path: Path = WATCHDOG_DB,
) -> int:
    conn = ensure_state_db(db_path)
    try:
        results: list[CheckResult] = []
        for check_name in check_names:
            if check_name == "recovery":
                results.append(evaluate_recovery(now=now))
            elif check_name == "backup":
                results.append(evaluate_backup(now=now))
            elif check_name == "system":
                results.append(evaluate_system())
            elif check_name == "access":
                results.append(evaluate_access(conn, learn_only=learn_access_only, now=now, debug=security_debug))
        exit_code = 0
        sent_alerts: list[str] = []
        for result in results:
            if result.status == "alert":
                send_now = should_send_alert(conn, result)
                if send_now:
                    send_alert(f"watchdog:{result.check_key}", result.message, dry_run=dry_run)
                    sent_alerts.append(result.check_key)
                print(f"WATCHDOG ALERT: {result.message}")
                # Ein erkannter Watchdog-Alert ist kein Script-/systemd-Fehler.
                # systemd soll nur fehlschlagen, wenn der Watchdog selbst crasht.
            elif result.status == "skip":
                should_send_alert(conn, result)
                print(f"WATCHDOG SKIP: {result.message}")
            else:
                should_send_alert(conn, result)
                print(f"WATCHDOG OK: {result.message}")
        if security_debug:
            print(f"SECURITY DEBUG ALERTS: {', '.join(sent_alerts) if sent_alerts else 'none'}")
        return exit_code
    finally:
        conn.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LIVA Watchdog")
    parser.add_argument("--check", choices=("recovery", "backup", "system", "access"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--learn-access-ips", action="store_true")
    parser.add_argument("--security-debug", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    check_names = [args.check] if args.check else ["recovery", "backup", "system", "access"]
    return run_checks(
        check_names,
        dry_run=bool(args.dry_run),
        learn_access_only=bool(args.learn_access_ips),
        security_debug=bool(args.security_debug),
    )


if __name__ == "__main__":
    raise SystemExit(main())
