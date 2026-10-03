# terminal_api.py
from __future__ import annotations

import os
import sqlite3
import time
import subprocess
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Set

from flask import Blueprint, jsonify
from flask import request

from database.sqlite_backup import latest_backup_timestamp

bp_about_stats = Blueprint("about_stats", __name__)


BASE_DIR = Path(__file__).resolve().parent
DB_DIR_DEFAULT = BASE_DIR / "database"

DB_ROLES_DEFAULT: Dict[str, str] = {
    "training": "training.sqlite3",
    "runs": "runs.sqlite3",
    "hrv": "hrv.sqlite3",
    "ernaehrung": "ernaehrung.sqlite3",
    "plans": "plans.sqlite3",
}

USB_BACKUP_DIR = Path("/mnt/usb/backups/sqlite")

_CODEBASE_CACHE: Dict[str, Any] = {"ts": 0.0, "lines": None, "root": None}
_ABOUT_CACHE: Dict[str, Any] = {"ts": 0.0, "payload": None}


def _now() -> datetime:
    return datetime.now()


def _fmt_date(d: Optional[date]) -> str:
    return d.isoformat() if d else "n/a"


def _fmt_dt_min(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "n/a"


def _is_repo_root(path: Path) -> bool:
    try:
        return path.is_dir() and (path / "app.py").exists()
    except Exception:
        return False


def _resolve_repo_root() -> str:
    env_root = (os.getenv("LIVA_REPO_ROOT") or "").strip()
    candidates = []
    if env_root:
        candidates.append(Path(env_root))
    candidates.extend(
        [
            BASE_DIR.parent,
            Path.cwd(),
            Path("/opt/liva"),
        ]
    )
    for candidate in candidates:
        if _is_repo_root(candidate):
            return str(candidate)
    return str(BASE_DIR.parent)


def _parse_dt_any(x: Any) -> Optional[datetime]:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x
    if isinstance(x, date):
        return datetime(x.year, x.month, x.day)

    if isinstance(x, (int, float)):
        try:
            return datetime.fromtimestamp(float(x))
        except Exception:
            return None

    s = str(x).strip()
    if not s:
        return None

    if s.endswith("Z"):
        s = s[:-1]

    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ]
    for f in fmts:
        try:
            return datetime.strptime(s[:19], f)
        except Exception:
            pass
    return None


# ----------------------------
# Codebase size (cached)
# ----------------------------

def _is_probably_binary(path: str, sniff_bytes: int = 8192) -> bool:
    # 1) Quick extension guard (nur wirklich „safe“ binary Endungen)
    ext = os.path.splitext(path)[1].lower()
    bin_exts = {
        ".sqlite", ".sqlite3", ".db", ".db3",
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico",
        ".pdf",
        ".zip", ".gz", ".tar", ".tgz", ".7z", ".rar",
        ".mp3", ".wav", ".mp4", ".mov", ".mkv",
        ".woff", ".woff2", ".ttf", ".otf",
        ".so", ".dylib", ".dll", ".exe", ".bin",
    }
    if ext in bin_exts:
        return True

    # 2) Content sniff: NUL-Bytes => sehr wahrscheinlich binary
    try:
        with open(path, "rb") as f:
            chunk = f.read(sniff_bytes)
        if b"\x00" in chunk:
            return True
    except Exception:
        # Wenn wir nicht lesen können: lieber skippen als crashen
        return True

    return False


def _iter_repo_files(
    root_dir: str,
    exclude_dirnames: Optional[Set[str]] = None,
    follow_symlinks: bool = False,
) -> Iterable[str]:
    # Minimal & sinnvoll: nur Sachen, die fast immer „nicht Code“ sind
    if exclude_dirnames is None:
        exclude_dirnames = {
            ".git",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".cache",
            "venv",
            ".venv",
            "node_modules",
            "dist",
            "build",
            ".idea",
            ".vscode",
        }

    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=True, followlinks=follow_symlinks):
        # prunes: nur nach *Ordnernamen* filtern (nicht nach dirpath-parts),
        # damit neue Ordnerstrukturen niemals „aus Versehen“ verschwinden.
        dirnames[:] = [
            d for d in dirnames
            if d not in exclude_dirnames and not (d.startswith(".") and d != ".well-known")
        ]

        for fn in filenames:
            p = os.path.join(dirpath, fn)
            try:
                # nur echte Dateien (keine broken links etc.)
                if not os.path.isfile(p):
                    continue
            except Exception:
                continue

            if _is_probably_binary(p):
                continue

            yield p


def _count_lines_streaming(path: str, chunk_size: int = 1024 * 1024) -> int:
    """
    Zählt Lines robust & speicherschonend:
    - zählt b'\\n'
    - falls Datei nicht mit \\n endet, zählt letzte Zeile extra
    """
    try:
        total_nl = 0
        last_byte = None

        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                total_nl += chunk.count(b"\n")
                last_byte = chunk[-1]

        if last_byte is None:
            return 0  # empty file
        if last_byte != 10:  # 10 == b"\n"
            return total_nl + 1
        return total_nl
    except Exception:
        return 0


def codebase_size_lines(repo_root: str, ttl_seconds: int = 3600) -> int:
    now = time.time()
    if (
        _CODEBASE_CACHE.get("lines") is not None
        and _CODEBASE_CACHE.get("root") == os.path.abspath(repo_root)
        and (now - float(_CODEBASE_CACHE.get("ts", 0.0))) < ttl_seconds
    ):
        return int(_CODEBASE_CACHE["lines"])

    total = 0
    for p in _iter_repo_files(repo_root):
        total += _count_lines_streaming(p)

    _CODEBASE_CACHE["ts"] = now
    _CODEBASE_CACHE["lines"] = int(total)
    _CODEBASE_CACHE["root"] = os.path.abspath(repo_root)
    return int(total)


# ----------------------------
# SQLite helpers
# ----------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _list_tables(con: sqlite3.Connection) -> List[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def _columns(con: sqlite3.Connection, table: str) -> List[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _safe_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _minmax_col(con: sqlite3.Connection, table: str, col: str) -> Tuple[Any, Any]:
    t = _safe_ident(table)
    c = _safe_ident(col)
    row = con.execute(
        f"SELECT MIN({c}) AS minv, MAX({c}) AS maxv FROM {t} WHERE {c} IS NOT NULL"
    ).fetchone()
    if not row:
        return None, None
    return row["minv"], row["maxv"]


def _minmax_dt_from_table(
    con: sqlite3.Connection, table: str, cols: Sequence[str]
) -> Tuple[Optional[datetime], Optional[datetime]]:
    cols_present = set(_columns(con, table))
    mins: List[datetime] = []
    maxs: List[datetime] = []
    for col in cols:
        if col not in cols_present:
            continue
        minv, maxv = _minmax_col(con, table, col)
        dmin = _parse_dt_any(minv)
        dmax = _parse_dt_any(maxv)
        if dmin:
            mins.append(dmin)
        if dmax:
            maxs.append(dmax)
    return (min(mins) if mins else None), (max(maxs) if maxs else None)


def _db_bounds_selected(db_path: Path, selection: Dict[str, Sequence[str]]) -> Tuple[Optional[datetime], Optional[datetime]]:
    if not db_path.exists():
        return None, None
    con = _connect(db_path)
    try:
        tables = set(_list_tables(con))
        mins: List[datetime] = []
        maxs: List[datetime] = []
        for table, cols in selection.items():
            if table not in tables:
                continue
            mn, mx = _minmax_dt_from_table(con, table, cols)
            if mn:
                mins.append(mn)
            if mx:
                maxs.append(mx)
        return (min(mins) if mins else None), (max(maxs) if maxs else None)
    finally:
        con.close()


def _quick_integrity_ok(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    con = _connect(db_path)
    try:
        row = con.execute("PRAGMA quick_check(1)").fetchone()
        return bool(row) and str(row[0]).strip().lower() == "ok"
    except Exception:
        return False
    finally:
        con.close()


# ----------------------------
# Stored values (atoms: non-null cells)
# ----------------------------

def _atoms_for_table(con: sqlite3.Connection, table: str, exclude: set[str]) -> int:
    cols = [c for c in _columns(con, table) if c not in exclude]
    if not cols:
        return 0
    parts = [f"({_safe_ident(c)} IS NOT NULL)" for c in cols]
    expr = " + ".join(parts)
    row = con.execute(f"SELECT SUM({expr}) AS atoms FROM {_safe_ident(table)}").fetchone()
    if not row or row["atoms"] is None:
        return 0
    return int(row["atoms"])


def _atoms_for_db(db_path: Path, table_exclude_map: Dict[str, set[str]]) -> int:
    if not db_path.exists():
        return 0
    con = _connect(db_path)
    try:
        atoms = 0
        tables = set(_list_tables(con))
        for table, exclude in table_exclude_map.items():
            if table in tables:
                atoms += _atoms_for_table(con, table, exclude)
        return atoms
    finally:
        con.close()


def _backup_info() -> Tuple[str, Optional[datetime]]:
    last_ts = latest_backup_timestamp()
    if last_ts is None:
        return "MISSING", None

    return "OK", _parse_dt_any(last_ts)


def _resolve_db_dir() -> Path:
    repo_root = BASE_DIR.parent
    root_db = repo_root / "database"
    if root_db.exists():
        return root_db
    return DB_DIR_DEFAULT


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _safe_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(float(x))
    except Exception:
        return None


def _epley_1rm(weight: float, reps: int) -> float:
    r = max(1, int(reps))
    w = max(0.0, float(weight))
    return w * (1.0 + (r / 30.0))


def _dotfill(label: str, width: int = 28) -> str:
    s = str(label)
    if len(s) >= width:
        return s
    return s + ("." * (width - len(s)))


def _sec_to_hms(seconds: Optional[int]) -> str:
    if seconds is None:
        return "n/a"
    s = max(0, int(seconds))
    hh = s // 3600
    mm = (s % 3600) // 60
    ss = s % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"

def _sec_to_mmss(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    s = max(0, int(round(float(seconds))))
    mm = s // 60
    ss = s % 60
    return f"{mm:02d}:{ss:02d}"


def _pace_str(seconds: int, km: float) -> str:
    if km <= 0:
        return "n/a"
    spk = float(seconds) / float(km)
    mm = int(spk // 60)
    ss = int(round(spk - mm * 60))
    if ss == 60:
        mm += 1
        ss = 0
    return f"{mm:02d}:{ss:02d} /km"


def _exercise_display_name(name: Optional[str], variation: Optional[str]) -> str:
    base = (name or "").strip()
    var = (variation or "").strip()
    if not base:
        return var or "n/a"
    if not var:
        return base
    base_lower = base.lower()
    var_lower = var.lower()
    if var_lower in base_lower:
        return base
    return f"{base} ({variation})"


def _parse_date_iso_utc(s: Any) -> Optional[date]:
    dt = _parse_dt_any(s)
    return dt.date() if dt else None


PR_STRENGTH_LIFT_DEFS: List[Dict[str, Any]] = [
    {
        "label": "Bench Press",
        "patterns": ["bench press", "bankdruecken", "bankdrücken", "bench"],
        "min_reps": 3,
        "exclude": ["bench row", "bench press row", "bench dips"],
    },
    {
        "label": "Incline Press",
        "patterns": ["incline press", "schraegbankdruecken", "schrägbankdrücken", "incline bench"],
        "min_reps": 3,
    },
    {
        "label": "Overhead Press",
        "patterns": ["overhead press", "ohp", "military press", "shoulder press"],
        "min_reps": 3,
    },
    {
        "label": "Squat",
        "patterns": ["squat", "kniebeuge", "front squat", "goblet squat", "hack squat", "back squat"],
        "min_reps": 3,
    },
    {
        "label": "Deadlift",
        "patterns": ["deadlift", "dead lift", "kreuzheben", "sumo deadlift", "rumänisches kreuzheben"],
        "min_reps": 1,
    },
    {
        "label": "Romanian Deadlift",
        "patterns": ["romanian deadlift", "rdl", "romanian", "rumänisches"],
        "min_reps": 3,
    },
    {
        "label": "Row",
        "patterns": ["barbell row", "dumbbell row", "row", "rowing", "t-bar row", "seated row"],
        "min_reps": 3,
        "exclude": ["bench row"],
    },
    {
        "label": "Lat Pulldown",
        "patterns": ["lat pulldown", "latzug", "pulldown", "pulldowns"],
        "min_reps": 6,
        "exclude": ["pull-up", "pull up"],
    },
    {
        "label": "Pull-Ups",
        "patterns": ["pull-up", "pull up", "pullups", "chin-up", "chin up", "klimmzug"],
        "min_reps": 1,
        "bodyweight": True,
    },
    {
        "label": "Dips",
        "patterns": ["dips", "dip"],
        "min_reps": 1,
        "bodyweight": True,
    },
    {
        "label": "Leg Press",
        "patterns": ["leg press", "beinpresse"],
        "min_reps": 6,
    },
    {
        "label": "Lateral Raise",
        "patterns": ["lateral raise", "seitheben", "side raise"],
        "min_reps": 8,
    },
]


def _best_strength_set(
    con: sqlite3.Connection,
    name_like_patterns: Sequence[str],
    min_reps: int = 3,
    exclude_patterns: Optional[Sequence[str]] = None,
    limit_rows: int = 24,
) -> Optional[Dict[str, Any]]:
    include_clause = " OR ".join(["LOWER(e.name) LIKE ?" for _ in name_like_patterns])
    params: List[Any] = [f"%{p.lower()}%" for p in name_like_patterns]

    where_clauses = [
        f"({include_clause})",
        "s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= ?",
    ]
    params.append(int(min_reps))

    if exclude_patterns:
        for pat in exclude_patterns:
            where_clauses.append("LOWER(e.name) NOT LIKE ?")
            params.append(f"%{pat.lower()}%")

    where_sql = "\n        AND ".join(where_clauses)
    sql = f"""
      SELECT
        e.name AS exercise_name,
        e.variation AS exercise_variation,
        COALESCE(CAST(s.weight AS REAL), 0) AS weight,
        CAST(s.reps AS INTEGER) AS reps,
        w.date_iso AS date_iso,
        w.date AS date_raw
      FROM sets s
      JOIN exercises e ON e.id = s.exercise_id
      JOIN workouts w ON w.id = s.workout_id
      WHERE {where_sql}
      ORDER BY
        COALESCE(CAST(s.weight AS REAL), 0) DESC,
        CAST(s.reps AS INTEGER) DESC,
        s.id DESC
      LIMIT {int(limit_rows)}
    """
    rows = con.execute(sql, params).fetchall()
    for row in rows:
        reps = _safe_int(row["reps"]) or 1
        weight = float(row["weight"] or 0.0)
        if weight <= 0 or reps <= 0:
            continue
        d = row["date_iso"] or row["date_raw"]
        d_iso = _parse_date_iso_utc(d)
        exercise_name = row["exercise_name"] or ""
        exercise_variation = row["exercise_variation"] or ""
        display = _exercise_display_name(exercise_name, exercise_variation)
        return {
            "weight": weight,
            "reps": reps,
            "e1rm": _epley_1rm(weight, reps),
            "date": _fmt_date(d_iso),
            "exercise_name": exercise_name,
            "exercise_variation": exercise_variation,
            "exercise_display": display,
        }
    return None

def _recent_strength_events(
    con: sqlite3.Connection,
    name_like_patterns: Sequence[str],
    limit_rows: int = 220,
    exclude_patterns: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    include_clause = " OR ".join(["LOWER(e.name) LIKE ?" for _ in name_like_patterns])
    params: List[Any] = [f"%{p.lower()}%" for p in name_like_patterns]
    where_clauses = [
        f"({include_clause})",
        "s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0",
    ]
    if exclude_patterns:
        for pat in exclude_patterns:
            where_clauses.append("LOWER(e.name) NOT LIKE ?")
            params.append(f"%{pat.lower()}%")

    where_sql = "\n        AND ".join(where_clauses)
    sql = f"""
      SELECT
        e.name AS exercise_name,
        e.variation AS exercise_variation,
        COALESCE(CAST(s.weight AS REAL), 0) AS weight,
        CAST(s.reps AS INTEGER) AS reps,
        COALESCE(w.date_iso, w.date) AS d
      FROM sets s
      JOIN exercises e ON e.id = s.exercise_id
      JOIN workouts w ON w.id = s.workout_id
      WHERE {where_sql}
      ORDER BY
        COALESCE(w.date_iso, w.date) DESC,
        COALESCE(CAST(s.weight AS REAL), 0) DESC,
        CAST(s.reps AS INTEGER) DESC,
        s.id DESC
      LIMIT {int(limit_rows)}
    """
    rows = con.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = _parse_date_iso_utc(r["d"])
        if not d:
            continue
        exercise_name = r["exercise_name"] or ""
        exercise_variation = r["exercise_variation"] or ""
        display = _exercise_display_name(exercise_name, exercise_variation)
        out.append(
            {
                "date": d,
                "weight": float(r["weight"] or 0.0),
                "reps": int(r["reps"] or 0),
                "exercise_name": exercise_name,
                "exercise_variation": exercise_variation,
                "exercise_display": display,
            }
        )
    return out


def _all_time_best_weight(con: sqlite3.Connection, patterns: Sequence[str], min_reps: int = 1) -> float:
    wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
    params = [f"%{p.lower()}%" for p in patterns]
    row = con.execute(
        f"""
        SELECT MAX(COALESCE(CAST(s.weight AS REAL),0)) AS mx
        FROM sets s
        JOIN exercises e ON e.id = s.exercise_id
        WHERE ({wh})
          AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= ?
        """,
        (*params, int(min_reps)),
    ).fetchone()
    if not row or row["mx"] is None:
        return 0.0
    return float(row["mx"] or 0.0)


def _all_time_near_best_max_reps(
    con: sqlite3.Connection, patterns: Sequence[str], best_weight: float, tol: float, min_reps: int = 1
) -> int:
    if best_weight <= 0:
        return 0
    wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
    params: List[Any] = [f"%{p.lower()}%" for p in patterns]
    params.append(int(min_reps))
    params.append(float(best_weight - tol))
    row = con.execute(
        f"""
        SELECT MAX(CAST(s.reps AS INTEGER)) AS mxr
        FROM sets s
        JOIN exercises e ON e.id = s.exercise_id
        WHERE ({wh})
          AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= ?
          AND COALESCE(CAST(s.weight AS REAL),0) >= ?
        """,
        params,
    ).fetchone()
    if not row or row["mxr"] is None:
        return 0
    return int(row["mxr"] or 0)


def _fmt_int(n: Optional[float]) -> str:
    if n is None:
        return "n/a"
    return f"{int(round(float(n))):,}"


def _fmt_float(n: Optional[float], digits: int = 1) -> str:
    if n is None:
        return "n/a"
    return f"{float(n):.{digits}f}"


def _pct_change(a: Optional[float], b: Optional[float]) -> str:
    """
    Percent change from b (previous) to a (current): (a-b)/b.
    """
    if a is None or b is None or b == 0:
        return "n/a"
    return f"{((float(a) - float(b)) / float(b)) * 100.0:+.1f}%"


def _delta(a: Optional[float], b: Optional[float], digits: int = 1) -> str:
    if a is None or b is None:
        return "n/a"
    return f"{(float(a) - float(b)):+.{digits}f}"


def _date_window(days: int) -> Tuple[date, date, date, date]:
    days = max(1, int(days))
    end_a = date.today()
    start_a = date.fromordinal(end_a.toordinal() - (days - 1))
    end_b = date.fromordinal(start_a.toordinal() - 1)
    start_b = date.fromordinal(end_b.toordinal() - (days - 1))
    return start_a, end_a, start_b, end_b


def _iso(d: date) -> str:
    return d.isoformat()


def parse_window(args: Sequence[str], now_date: date, default_days: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Global window parser.
    Supports:
      --days N
      --since YYYY-MM-DD
    Rules:
      - If both provided: error (explicit).
      - If neither: default_days.
      - End date is inclusive and equals now_date.
    Returns (window, error).
    window = {start_date, end_date, label, days}
    """
    low = [str(a).lower() for a in args]
    has_days = "--days" in low
    has_since = "--since" in low

    if has_days and has_since:
        return None, "Use either --days or --since, not both."

    end_date = now_date

    if has_since:
        try:
            i = low.index("--since")
            start_date = date.fromisoformat(str(args[i + 1]))
        except Exception:
            return None, "Invalid --since value. Expected YYYY-MM-DD."
        days = max(1, (end_date - start_date).days + 1)
        label = f"since {start_date.isoformat()} ({start_date.isoformat()} -> {end_date.isoformat()})"
        return {"start_date": start_date, "end_date": end_date, "days": days, "label": label}, None

    if has_days:
        try:
            i = low.index("--days")
            days = int(str(args[i + 1]))
            if days <= 0:
                raise ValueError()
        except Exception:
            return None, "Invalid --days value. Expected an integer > 0."
    else:
        days = int(default_days)

    start_date = date.fromordinal(end_date.toordinal() - (days - 1))
    label = f"last {days} days ({start_date.isoformat()} -> {end_date.isoformat()})"
    return {"start_date": start_date, "end_date": end_date, "days": days, "label": label}, None


def _prev_window(window: Dict[str, Any]) -> Dict[str, Any]:
    days = int(window["days"])
    end_b = date.fromordinal(window["start_date"].toordinal() - 1)
    start_b = date.fromordinal(end_b.toordinal() - (days - 1))
    label = f"previous {days} days ({start_b.isoformat()} -> {end_b.isoformat()})"
    return {"start_date": start_b, "end_date": end_b, "days": days, "label": label}


def _window_line(window: Dict[str, Any]) -> str:
    return f"{_dotfill('Window', 28)} {window['label']}"


def _db_paths() -> Dict[str, Path]:
    db_dir = _resolve_db_dir()
    roles = dict(DB_ROLES_DEFAULT)
    return {k: (db_dir / v) for k, v in roles.items()}


def _strength_totals(con: sqlite3.Connection, window: Dict[str, Any]) -> Dict[str, Any]:
    start = _iso(window["start_date"])
    end = _iso(window["end_date"])

    row = con.execute(
        "SELECT COUNT(*) AS n, MIN(date_iso) AS mn, MAX(date_iso) AS mx FROM workouts WHERE date_iso BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    sessions = int(row["n"] or 0)
    cov_min = row["mn"]
    cov_max = row["mx"]

    row = con.execute(
        """
        SELECT
          COUNT(*) AS n,
          SUM(COALESCE(CAST(s.weight AS REAL),0) * COALESCE(CAST(s.reps AS REAL),0)) AS ton
        FROM sets s
        JOIN workouts w ON w.id = s.workout_id
        WHERE w.date_iso BETWEEN ? AND ?
          AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
        """,
        (start, end),
    ).fetchone()
    total_sets = int(row["n"] or 0)
    tonnage = float(row["ton"] or 0.0)

    # minutes are not stored -> estimate (deterministic)
    est_minutes = float(total_sets) * 6.0

    return {
        "sessions": sessions,
        "total_sets": total_sets,
        "tonnage_kg": tonnage,
        "est_minutes": est_minutes,
        "coverage_min": cov_min,
        "coverage_max": cov_max,
    }


def _running_totals(con: sqlite3.Connection, window: Dict[str, Any]) -> Dict[str, Any]:
    start = _iso(window["start_date"])
    end = _iso(window["end_date"])
    row = con.execute(
        "SELECT COUNT(*) AS n, SUM(distance) AS s FROM runs WHERE substr(date,1,10) BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    return {"runs": int(row["n"] or 0), "distance_km": float(row["s"] or 0.0) / 1000.0}


def _hrv_totals(con: sqlite3.Connection, window: Dict[str, Any]) -> Dict[str, Any]:
    start = _iso(window["start_date"])
    end = _iso(window["end_date"])
    row = con.execute(
        "SELECT COUNT(DISTINCT substr(date_utc,1,10)) AS n FROM hrv_measurements WHERE substr(date_utc,1,10) BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    return {"coverage_days": int(row["n"] or 0)}


def _build_progress_lines(days: int) -> List[str]:
    days = max(7, min(365, int(days)))
    start_a, end_a, start_b, end_b = _date_window(days)

    db_dir = _resolve_db_dir()
    roles = dict(DB_ROLES_DEFAULT)
    p_training = db_dir / roles["training"]
    p_runs = db_dir / roles["runs"]
    p_hrv = db_dir / roles["hrv"]

    # -------------------------
    # STRENGTH
    # -------------------------
    sessions_a = sessions_b = 0
    sets_a = sets_b = 0
    ton_a = ton_b = 0.0
    avg_top_load_a = avg_top_load_b = None  # percent as float
    avg_rpe_a = avg_rpe_b = None
    tech_a = tech_b = None

    verified_pr_a = verified_pr_b = 0
    rep_pr_a = rep_pr_b = 0
    retention_a = retention_b = None

    if p_training.exists():
        con = _connect(p_training)
        try:
            # sessions
            row = con.execute(
                "SELECT COUNT(*) AS n FROM workouts WHERE date_iso BETWEEN ? AND ?",
                (_iso(start_a), _iso(end_a)),
            ).fetchone()
            sessions_a = int(row["n"] or 0)
            row = con.execute(
                "SELECT COUNT(*) AS n FROM workouts WHERE date_iso BETWEEN ? AND ?",
                (_iso(start_b), _iso(end_b)),
            ).fetchone()
            sessions_b = int(row["n"] or 0)

            # sets + tonnage
            row = con.execute(
                """
                SELECT
                  COUNT(*) AS n,
                  SUM(COALESCE(CAST(s.weight AS REAL),0) * COALESCE(CAST(s.reps AS REAL),0)) AS ton
                FROM sets s
                JOIN workouts w ON w.id = s.workout_id
                WHERE w.date_iso BETWEEN ? AND ?
                  AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                """,
                (_iso(start_a), _iso(end_a)),
            ).fetchone()
            sets_a = int(row["n"] or 0)
            ton_a = float(row["ton"] or 0.0)

            row = con.execute(
                """
                SELECT
                  COUNT(*) AS n,
                  SUM(COALESCE(CAST(s.weight AS REAL),0) * COALESCE(CAST(s.reps AS REAL),0)) AS ton
                FROM sets s
                JOIN workouts w ON w.id = s.workout_id
                WHERE w.date_iso BETWEEN ? AND ?
                  AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                """,
                (_iso(start_b), _iso(end_b)),
            ).fetchone()
            sets_b = int(row["n"] or 0)
            ton_b = float(row["ton"] or 0.0)

            # avg top-set load (compound): compare weighted mean top-set weight per workout
            compound = ["bench", "squat", "deadlift", "rdl", "overhead press", "ohp", "dip", "pull-up", "chin-up"]
            wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in compound])
            params = [f"%{p}%" for p in compound]

            def top_sets_between(a: date, b: date) -> List[sqlite3.Row]:
                return con.execute(
                    f"""
                    WITH c AS (
                      SELECT
                        w.id AS workout_id,
                        MAX(COALESCE(CAST(s.weight AS REAL),0)) AS top_w
                      FROM sets s
                      JOIN exercises e ON e.id = s.exercise_id
                      JOIN workouts w ON w.id = s.workout_id
                      WHERE w.date_iso BETWEEN ? AND ?
                        AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= 3
                        AND ({wh})
                      GROUP BY w.id
                    )
                    SELECT
                      w.date_iso AS d,
                      e.name AS name,
                      e.variation AS variation,
                      COALESCE(CAST(s.weight AS REAL),0) AS weight,
                      CAST(s.reps AS INTEGER) AS reps,
                      CAST(s.rpe AS REAL) AS rpe
                    FROM sets s
                    JOIN exercises e ON e.id = s.exercise_id
                    JOIN workouts w ON w.id = s.workout_id
                    JOIN c ON c.workout_id = w.id AND COALESCE(CAST(s.weight AS REAL),0) = c.top_w
                    WHERE w.date_iso BETWEEN ? AND ?
                      AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= 3
                      AND ({wh})
                    ORDER BY w.date_iso DESC, s.id DESC
                    """,
                    (_iso(a), _iso(b), *params, _iso(a), _iso(b), *params),
                ).fetchall()

            def weighted_mean_top_weight(rows: List[sqlite3.Row]) -> Optional[float]:
                if not rows:
                    return None
                num = 0.0
                den = 0.0
                for r in rows:
                    w = float(r["weight"] or 0.0)
                    reps = float(r["reps"] or 0.0)
                    if w <= 0 or reps <= 0:
                        continue
                    num += w * reps
                    den += reps
                if den <= 0:
                    return None
                return num / den

            rows_a = top_sets_between(start_a, end_a)
            rows_b = top_sets_between(start_b, end_b)
            m_a = weighted_mean_top_weight(rows_a)
            m_b = weighted_mean_top_weight(rows_b)
            if m_a is not None and m_b is not None and m_b > 0:
                avg_top_load_a = (m_a / m_b - 1.0) * 100.0

            def mean_rpe(rows: List[sqlite3.Row]) -> Optional[float]:
                vals = [float(r["rpe"]) for r in rows if r["rpe"] is not None]
                if not vals:
                    return None
                return sum(vals) / len(vals)

            avg_rpe_a = mean_rpe(rows_a)
            avg_rpe_b = mean_rpe(rows_b)

            def technique_consistency(rows: List[sqlite3.Row]) -> Optional[float]:
                if not rows:
                    return None
                keys: List[str] = []
                for r in rows:
                    name = (r["name"] or "").strip().lower()
                    var = (r["variation"] or "").strip().lower()
                    k = f"{name}::{var}" if var else name
                    if k:
                        keys.append(k)
                if not keys:
                    return None
                from collections import Counter

                c = Counter(keys)
                total = sum(c.values())
                top = max(c.values()) if total > 0 else 0
                return float(top) / float(total) if total > 0 else None

            tech_a = technique_consistency(rows_a)
            tech_b = technique_consistency(rows_b)

            # PR counts + retention: based on key lifts
            lift_defs_full: Dict[str, Sequence[str]] = {
                "Bench Press": ["bench press", "bench", "bank"],
                "Romanian Deadlift": ["romanian deadlift", "rdl"],
                "Dips": ["dips", "dip"],
                "Pull-Ups": ["pull-up", "pull up", "pullups", "chin-up", "chin up"],
                "Overhead Press": ["overhead press", "ohp", "military press", "shoulder press"],
            }

            def _best_weight_and_date(patterns: Sequence[str]) -> Tuple[float, Optional[date]]:
                wh2 = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
                params2 = [f"%{p.lower()}%" for p in patterns]
                row = con.execute(
                    f"""
                    SELECT
                      COALESCE(CAST(s.weight AS REAL),0) AS w,
                      CAST(s.reps AS INTEGER) AS reps,
                      COALESCE(wk.date_iso, wk.date) AS d
                    FROM sets s
                    JOIN exercises e ON e.id = s.exercise_id
                    JOIN workouts wk ON wk.id = s.workout_id
                    WHERE ({wh2})
                      AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= 3
                    ORDER BY COALESCE(CAST(s.weight AS REAL),0) DESC, CAST(s.reps AS INTEGER) DESC, s.id DESC
                    LIMIT 1
                    """,
                    params2,
                ).fetchone()
                if not row:
                    return 0.0, None
                return float(row["w"] or 0.0), _parse_date_iso_utc(row["d"])

            def _near_best_rep_and_date(patterns: Sequence[str], best_w: float, tol: float = 2.5) -> Tuple[int, Optional[date]]:
                wh2 = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
                params2 = [f"%{p.lower()}%" for p in patterns]
                params2.append(float(best_w - tol))
                row = con.execute(
                    f"""
                    SELECT
                      CAST(s.reps AS INTEGER) AS reps,
                      COALESCE(wk.date_iso, wk.date) AS d,
                      COALESCE(CAST(s.weight AS REAL),0) AS w
                    FROM sets s
                    JOIN exercises e ON e.id = s.exercise_id
                    JOIN workouts wk ON wk.id = s.workout_id
                    WHERE ({wh2})
                      AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                      AND COALESCE(CAST(s.weight AS REAL),0) >= ?
                    ORDER BY CAST(s.reps AS INTEGER) DESC, COALESCE(CAST(s.weight AS REAL),0) DESC, s.id DESC
                    LIMIT 1
                    """,
                    params2,
                ).fetchone()
                if not row:
                    return 0, None
                return int(row["reps"] or 0), _parse_date_iso_utc(row["d"])

            all_time_e1rms: List[float] = []
            win_a_e1rms: List[float] = []
            win_b_e1rms: List[float] = []
            for patterns in lift_defs_full.values():
                best_w, best_d = _best_weight_and_date(patterns)
                if best_d and start_a <= best_d <= end_a:
                    verified_pr_a += 1
                if best_d and start_b <= best_d <= end_b:
                    verified_pr_b += 1

                rep_best, rep_d = _near_best_rep_and_date(patterns, best_w, tol=2.5)
                if rep_d and start_a <= rep_d <= end_a:
                    rep_pr_a += 1
                if rep_d and start_b <= rep_d <= end_b:
                    rep_pr_b += 1

                # retention via e1rm best in windows vs all time (using epley on top weight+reps in that window)
                # all-time e1rm
                wh2 = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
                params2 = [f"%{p.lower()}%" for p in patterns]
                row = con.execute(
                    f"""
                    SELECT
                      MAX(COALESCE(CAST(s.weight AS REAL),0) * (1.0 + (CAST(s.reps AS REAL)/30.0))) AS mx
                    FROM sets s
                    JOIN exercises e ON e.id = s.exercise_id
                    WHERE ({wh2})
                      AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                    """,
                    params2,
                ).fetchone()
                all_mx = float(row["mx"] or 0.0)
                if all_mx > 0:
                    all_time_e1rms.append(all_mx)

                def best_e1rm_between(a: date, b: date) -> float:
                    row2 = con.execute(
                        f"""
                        SELECT
                          MAX(COALESCE(CAST(s.weight AS REAL),0) * (1.0 + (CAST(s.reps AS REAL)/30.0))) AS mx
                        FROM sets s
                        JOIN exercises e ON e.id = s.exercise_id
                        JOIN workouts wk ON wk.id = s.workout_id
                        WHERE ({wh2})
                          AND wk.date_iso BETWEEN ? AND ?
                          AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                        """,
                        (*params2, _iso(a), _iso(b)),
                    ).fetchone()
                    return float(row2["mx"] or 0.0)

                win_a_e1rms.append(best_e1rm_between(start_a, end_a))
                win_b_e1rms.append(best_e1rm_between(start_b, end_b))

            # compute mean retention across lifts (ignore zeros)
            def retention(win: List[float], allv: List[float]) -> Optional[float]:
                ratios: List[float] = []
                for wv, av in zip(win, allv):
                    if av > 0 and wv > 0:
                        ratios.append(wv / av)
                if not ratios:
                    return None
                return (sum(ratios) / len(ratios)) * 100.0

            retention_a = retention(win_a_e1rms, all_time_e1rms)
            retention_b = retention(win_b_e1rms, all_time_e1rms)
        finally:
            con.close()

    # -------------------------
    # RUNNING
    # -------------------------
    runs_a = runs_b = 0
    dist_a = dist_b = 0.0
    pace_z2_a = pace_z2_b = None  # seconds per km
    prox_5k_a = prox_5k_b = None  # percent

    if p_runs.exists():
        con = _connect(p_runs)
        try:
            row = con.execute(
                "SELECT COUNT(*) AS n, SUM(distance) AS s FROM runs WHERE substr(date,1,10) BETWEEN ? AND ?",
                (_iso(start_a), _iso(end_a)),
            ).fetchone()
            runs_a = int(row["n"] or 0)
            dist_a = float(row["s"] or 0.0) / 1000.0
            row = con.execute(
                "SELECT COUNT(*) AS n, SUM(distance) AS s FROM runs WHERE substr(date,1,10) BETWEEN ? AND ?",
                (_iso(start_b), _iso(end_b)),
            ).fetchone()
            runs_b = int(row["n"] or 0)
            dist_b = float(row["s"] or 0.0) / 1000.0

            # Z2 pace = avg pace for avg_hr in [120..150]
            def z2_pace(a: date, b: date) -> Optional[float]:
                rows = con.execute(
                    """
                    SELECT distance AS dist, moving_time AS t
                    FROM runs
                    WHERE substr(date,1,10) BETWEEN ? AND ?
                      AND avg_hr IS NOT NULL AND avg_hr BETWEEN 120 AND 150
                      AND distance IS NOT NULL AND distance > 0
                      AND moving_time IS NOT NULL AND moving_time > 0
                    """,
                    (_iso(a), _iso(b)),
                ).fetchall()
                if not rows:
                    return None
                tot_sec = 0.0
                tot_km = 0.0
                for r in rows:
                    tot_sec += float(r["t"])
                    tot_km += float(r["dist"]) / 1000.0
                if tot_km <= 0:
                    return None
                return tot_sec / tot_km

            pace_z2_a = z2_pace(start_a, end_a)
            pace_z2_b = z2_pace(start_b, end_b)

            # best 5k proximity
            row_best = con.execute(
                """
                SELECT MIN(moving_time) AS t
                FROM runs
                WHERE distance BETWEEN 4700 AND 5300
                  AND moving_time IS NOT NULL AND moving_time > 0
                """
            ).fetchone()
            best_all = int(row_best["t"] or 0)
            if best_all > 0:
                def best_5k_in(a: date, b: date) -> int:
                    row2 = con.execute(
                        """
                        SELECT MIN(moving_time) AS t
                        FROM runs
                        WHERE substr(date,1,10) BETWEEN ? AND ?
                          AND distance BETWEEN 4700 AND 5300
                          AND moving_time IS NOT NULL AND moving_time > 0
                        """,
                        (_iso(a), _iso(b)),
                    ).fetchone()
                    return int(row2["t"] or 0)

                a_best = best_5k_in(start_a, end_a)
                b_best = best_5k_in(start_b, end_b)
                if a_best > 0:
                    prox_5k_a = ((a_best - best_all) / best_all) * 100.0
                if b_best > 0:
                    prox_5k_b = ((b_best - best_all) / best_all) * 100.0
        finally:
            con.close()

    # -------------------------
    # RECOVERY
    # -------------------------
    rmssd_a = rmssd_b = None
    hr_a = hr_b = None
    stab_a = stab_b = None

    if p_hrv.exists():
        con = _connect(p_hrv)
        try:
            def mean_std(col: str, a: date, b: date) -> Tuple[Optional[float], Optional[float]]:
                row = con.execute(
                    f"""
                    SELECT AVG({col}) AS m, AVG(({col})*({col})) AS m2
                    FROM hrv_measurements
                    WHERE substr(date_utc,1,10) BETWEEN ? AND ?
                      AND {col} IS NOT NULL AND {col} > 0
                    """,
                    (_iso(a), _iso(b)),
                ).fetchone()
                if not row or row["m"] is None:
                    return None, None
                m = float(row["m"])
                m2 = float(row["m2"] or 0.0)
                var = max(0.0, m2 - m * m)
                return m, var ** 0.5

            rmssd_a, rmssd_std_a = mean_std("rmssd", start_a, end_a)
            rmssd_b, rmssd_std_b = mean_std("rmssd", start_b, end_b)
            hr_a, hr_std_a = mean_std("hr", start_a, end_a)
            hr_b, hr_std_b = mean_std("hr", start_b, end_b)

            def stability(m: Optional[float], sd: Optional[float]) -> Optional[float]:
                if m is None or sd is None or m <= 0:
                    return None
                cv = sd / m
                return 1.0 / (1.0 + cv)

            stab_a = stability(rmssd_a, rmssd_std_a)
            stab_b = stability(rmssd_b, rmssd_std_b)
        finally:
            con.close()

    # -------------------------
    # Output
    # -------------------------
    lines: List[str] = []
    lines.append("PROGRESS REPORT")
    lines.append(f"{_dotfill('Window A', 28)} last {days} days")
    lines.append(f"{_dotfill('Window B', 28)} previous {days} days")
    lines.append("")
    lines.append("STRENGTH")
    lines.append(f"{_dotfill('Sessions', 28)} {sessions_a}  vs  {sessions_b}          ({_pct_change(sessions_a, sessions_b)})")
    lines.append(f"{_dotfill('Total sets', 28)} {sets_a} vs  {sets_b}         ({_pct_change(sets_a, sets_b)})")
    lines.append(
        f"{_dotfill('Total tonnage', 28)} {_fmt_int(ton_a)} kg vs {_fmt_int(ton_b)} kg ({_pct_change(ton_a, ton_b)})"
    )
    if avg_top_load_a is None:
        lines.append(f"{_dotfill('Avg top-set load (compound)', 28)} n/a")
    else:
        lines.append(f"{_dotfill('Avg top-set load (compound)', 28)} {avg_top_load_a:+.1f}% (weighted mean)")
    if avg_rpe_a is None or avg_rpe_b is None:
        lines.append(f"{_dotfill('Avg RPE (top sets)', 28)} n/a")
    else:
        lines.append(f"{_dotfill('Avg RPE (top sets)', 28)} {avg_rpe_a:.1f} -> {avg_rpe_b:.1f}          ({_delta(avg_rpe_b, avg_rpe_a, 1)})")
    if tech_a is None or tech_b is None:
        lines.append(f"{_dotfill('Technique consistency', 28)} n/a")
    else:
        lines.append(f"{_dotfill('Technique consistency', 28)} {tech_a:.2f} -> {tech_b:.2f}        ({_delta(tech_b, tech_a, 2)})")

    lines.append("")
    lines.append("PR / PERFORMANCE")
    lines.append(f"{_dotfill('Verified PR count', 28)} {verified_pr_a} vs {verified_pr_b}")
    lines.append(f"{_dotfill('Rep PR count', 28)} {rep_pr_a} vs {rep_pr_b}")
    if retention_a is None or retention_b is None:
        lines.append(f"{_dotfill('Peak retention', 28)} n/a")
    else:
        lines.append(f"{_dotfill('Peak retention', 28)} {retention_a:.0f}% -> {retention_b:.0f}%          ({_delta(retention_b, retention_a, 0)}%)")

    lines.append("")
    lines.append("RUNNING")
    lines.append(f"{_dotfill('Runs logged', 28)} {runs_a} vs {runs_b}            ({_pct_change(runs_a, runs_b)})")
    lines.append(
        f"{_dotfill('Distance', 28)} {_fmt_float(dist_a,1)} km vs {_fmt_float(dist_b,1)} km ({_pct_change(dist_a, dist_b)})"
    )
    if pace_z2_a is None or pace_z2_b is None:
        lines.append(f"{_dotfill('Avg pace (Z2 runs)', 28)} n/a")
    else:
        delta_sec = int(round(pace_z2_b - pace_z2_a))
        sign = "-" if delta_sec < 0 else "+"
        ds = abs(delta_sec)
        lines.append(
            f"{_dotfill('Avg pace (Z2 runs)', 28)} {_sec_to_mmss(pace_z2_a)} -> {_sec_to_mmss(pace_z2_b)}     ({sign}{_sec_to_mmss(ds)}/km)"
        )
    if prox_5k_a is None or prox_5k_b is None:
        lines.append(f"{_dotfill('Best 5k proximity', 28)} n/a")
    else:
        closer = "closer to PR" if prox_5k_b < prox_5k_a else "farther from PR"
        lines.append(f"{_dotfill('Best 5k proximity', 28)} {prox_5k_a:.1f}% -> {prox_5k_b:.1f}%       ({closer})")

    lines.append("")
    lines.append("RECOVERY (HRV / BPM)")
    if rmssd_a is None and rmssd_b is None:
        lines.append(f"{_dotfill('RMSSD baseline', 28)} n/a")
    else:
        left = "n/a" if rmssd_a is None else str(int(round(rmssd_a)))
        right = "n/a" if rmssd_b is None else str(int(round(rmssd_b)))
        extra = "" if (rmssd_a is None or rmssd_b is None) else f"          ({_delta(rmssd_b, rmssd_a, 0)} ms)"
        lines.append(f"{_dotfill('RMSSD baseline', 28)} {left} -> {right}{extra}")

    if hr_a is None and hr_b is None:
        lines.append(f"{_dotfill('Resting HR (rolling)', 28)} n/a")
    else:
        left = "n/a" if hr_a is None else str(int(round(hr_a)))
        right = "n/a" if hr_b is None else str(int(round(hr_b)))
        extra = "" if (hr_a is None or hr_b is None) else f"            ({_delta(hr_b, hr_a, 0)} bpm)"
        lines.append(f"{_dotfill('Resting HR (rolling)', 28)} {left} -> {right}{extra}")

    if stab_a is None and stab_b is None:
        lines.append(f"{_dotfill('Readiness stability', 28)} n/a")
    else:
        left = "n/a" if stab_a is None else f"{stab_a:.2f}"
        right = "n/a" if stab_b is None else f"{stab_b:.2f}"
        extra = "" if (stab_a is None or stab_b is None) else f"       ({_delta(stab_b, stab_a, 2)})"
        lines.append(f"{_dotfill('Readiness stability', 28)} {left} -> {right}{extra}")

    lines.append("")
    lines.append("INTERPRETATION (data only)")
    bullets: List[str] = []

    # Volume direction (majority vote across sessions/sets/tonnage)
    down = int(sessions_a < sessions_b) + int(sets_a < sets_b) + int(ton_a < ton_b)
    up = int(sessions_a > sessions_b) + int(sets_a > sets_b) + int(ton_a > ton_b)
    if sessions_a == 0 and sessions_b == 0 and sets_a == 0 and sets_b == 0:
        bullets.append("No strength data in either window")
    else:
        vol = "Volume flat"
        if down >= 2:
            vol = "Volume down"
        elif up >= 2:
            vol = "Volume up"

        if avg_top_load_a is not None:
            if avg_top_load_a >= 2.0 and vol == "Volume down":
                bullets.append("Intensity up, volume down")
            elif avg_top_load_a <= -2.0 and vol == "Volume up":
                bullets.append("Intensity down, volume up")
            elif avg_top_load_a >= 2.0:
                bullets.append("Intensity up")
            elif avg_top_load_a <= -2.0:
                bullets.append("Intensity down")
            else:
                bullets.append(vol)
        else:
            bullets.append(vol)

    # Recovery direction (only when both signals exist)
    # NOTE: interpret the printed "A -> B" arrow as the direction for deltas.
    if rmssd_a is not None and rmssd_b is not None and hr_a is not None and hr_b is not None:
        rmssd_delta = float(rmssd_b) - float(rmssd_a)
        hr_delta = float(hr_b) - float(hr_a)
        if rmssd_delta < 0 and hr_delta > 0:
            bullets.append("Recovery trending down")
        elif rmssd_delta > 0 and hr_delta < 0:
            bullets.append("Recovery trending up")
        else:
            bullets.append("Recovery mixed")

    # Stability direction (only when available, and keep total 2–3 lines)
    if len(bullets) < 3 and stab_a is not None and stab_b is not None:
        if abs(stab_a - stab_b) < 0.03:
            bullets.append("Stability unchanged")
        elif stab_a > stab_b:
            bullets.append("Stability up")
        else:
            bullets.append("Stability down")

    for b in bullets[:3]:
        lines.append(f"- {b}")
    return lines


def _build_capacity_lines(window: Dict[str, Any]) -> List[str]:
    paths = _db_paths()
    p_training = paths["training"]

    sessions = 0
    total_sets = 0
    tonnage = 0.0
    est_minutes = 0.0

    hard = med = easy = unknown = 0

    peak_count = 0
    peaks_evaluated = 0
    peaks_excluded = 0
    drop_samples = 0
    rec_samples = 0
    perf_drop = None
    rec_days = None

    if p_training.exists():
        con = _connect(p_training)
        try:
            totals = _strength_totals(con, window)
            sessions = int(totals["sessions"])
            total_sets = int(totals["total_sets"])
            tonnage = float(totals["tonnage_kg"])
            est_minutes = float(totals["est_minutes"])

            # LOAD STRUCTURE: classify each session exactly once by max RPE (unknown if none)
            rows = con.execute(
                """
                SELECT
                  w.id AS workout_id,
                  MAX(CAST(s.rpe AS REAL)) AS max_rpe
                FROM workouts w
                LEFT JOIN sets s ON s.workout_id = w.id
                WHERE w.date_iso BETWEEN ? AND ?
                GROUP BY w.id
                """,
                (_iso(window["start_date"]), _iso(window["end_date"])),
            ).fetchall()

            for r in rows:
                v = _safe_float(r["max_rpe"])
                if v is None:
                    unknown += 1
                elif v >= 8.5:
                    hard += 1
                elif v >= 7.0:
                    med += 1
                else:
                    easy += 1

            # FATIGUE RESPONSE:
            # Per-lift daily best e1RM -> peaks (21d local maxima), then within-lift drop + recovery.
            lift_defs: Dict[str, Sequence[str]] = {
                "Bench": ["bench press", "bench", "bank"],
                "RDL": ["romanian deadlift", "rdl", "romanian"],
                "Squat": ["squat", "kniebeuge"],
                "OHP": ["overhead press", "ohp", "military press", "shoulder press", "schulterdrücken"],
                "Dips": ["dips", "dip"],
                "Pull-Ups": ["pull-up", "pull up", "pullups", "chin-up", "chin up", "klimmzug"],
            }

            all_drop: List[float] = []
            all_rec: List[float] = []
            total_peaks = 0
            total_eval = 0
            total_excl = 0

            for _lift, patterns in lift_defs.items():
                wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
                params = [f"%{p.lower()}%" for p in patterns]
                rows = con.execute(
                    f"""
                    SELECT
                      w.date_iso AS d,
                      MAX(COALESCE(CAST(s.weight AS REAL),0) * (1.0 + (CAST(s.reps AS REAL)/30.0))) AS e1
                    FROM sets s
                    JOIN exercises e ON e.id = s.exercise_id
                    JOIN workouts w ON w.id = s.workout_id
                    WHERE w.date_iso BETWEEN ? AND ?
                      AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                      AND ({wh})
                    GROUP BY w.date_iso
                    ORDER BY w.date_iso ASC
                    """,
                    (_iso(window["start_date"]), _iso(window["end_date"]), *params),
                ).fetchall()

                series: List[Tuple[date, float]] = []
                for rr in rows:
                    d = _parse_date_iso_utc(rr["d"])
                    v = _safe_float(rr["e1"])
                    if d and v and v > 0:
                        series.append((d, float(v)))

                if len(series) < 8:
                    continue

                series.sort(key=lambda x: x[0])

                # EMA(14) baseline per lift
                alpha = 2.0 / (14.0 + 1.0)
                ema: List[float] = []
                for i, (_, v) in enumerate(series):
                    if i == 0:
                        ema.append(v)
                    else:
                        ema.append(alpha * v + (1.0 - alpha) * ema[-1])

                peaks: List[int] = []
                for i, (di, vi) in enumerate(series):
                    wmax = max(v for (d, v) in series if abs((d - di).days) <= 10)
                    if vi >= wmax - 1e-9:
                        peaks.append(i)

                # de-dup flat plateaus: keep the last index per plateau
                peaks2: List[int] = []
                prev_i = None
                for i in peaks:
                    if prev_i is None:
                        prev_i = i
                        continue
                    if i == prev_i + 1 and abs(series[i][1] - series[prev_i][1]) <= 1e-9:
                        prev_i = i
                    else:
                        peaks2.append(prev_i)
                        prev_i = i
                if prev_i is not None:
                    peaks2.append(prev_i)

                total_peaks += len(peaks2)
                for i in peaks2:
                    peak_date, peak_v = series[i]
                    base = ema[i - 1] if i > 0 else peak_v
                    target = max(peak_v * 0.98, base)

                    next7 = [v for (d, v) in series if 1 <= (d - peak_date).days <= 7]
                    if not next7:
                        total_excl += 1
                        continue

                    total_eval += 1
                    mn = min(next7)
                    if peak_v > 0:
                        all_drop.append((peak_v - mn) / peak_v * 100.0)

                    for d, v in series:
                        dd = (d - peak_date).days
                        if dd <= 0:
                            continue
                        if dd > 30:
                            break
                        if v >= target:
                            all_rec.append(float(dd))
                            break

            peak_count = total_peaks
            peaks_evaluated = total_eval
            peaks_excluded = total_excl
            drop_samples = len(all_drop)
            rec_samples = len(all_rec)
            if all_drop:
                all_drop.sort()
                perf_drop = all_drop[len(all_drop) // 2]
            if all_rec:
                all_rec.sort()
                rec_days = all_rec[len(all_rec) // 2]
        finally:
            con.close()

    weeks = float(window["days"]) / 7.0
    sess_per_week = (sessions / weeks) if weeks > 0 else 0.0
    min_per_week = (est_minutes / weeks) if weeks > 0 else 0.0
    sets_per_sess = (total_sets / sessions) if sessions > 0 else 0.0
    ton_per_week = (tonnage / weeks) if weeks > 0 else 0.0

    def clamp(v: float, a: float, b: float) -> int:
        return int(round(max(a, min(b, v))))

    # internal scores (bounded + monotonic with fatigue metrics)
    work_capacity = clamp((sess_per_week / 4.0) * 40.0 + (min_per_week / 240.0) * 40.0 + (ton_per_week / 60000.0) * 20.0, 0, 100)
    fatigue_mgmt = clamp(90.0 - min(float(perf_drop or 0.0), 25.0) * 1.4 - min(float(rec_days or 0.0), 14.0) * 3.5, 0, 100)
    consistency = clamp((min(sess_per_week, 4.0) / 4.0) * 60.0 + (1.0 - min((hard / max(1, sessions)), 0.7)) * 40.0, 0, 100)

    lines: List[str] = []
    lines.append(f"WORK CAPACITY — {window['label']}")
    lines.append(_window_line(window))
    lines.append("")
    lines.append("DENSITY")
    lines.append(f"{_dotfill('Sessions', 28)} {sessions}")
    lines.append(f"{_dotfill('Total sets', 28)} {total_sets}")
    lines.append(f"{_dotfill('Total tonnage', 28)} {_fmt_int(tonnage)} kg")
    lines.append(f"{_dotfill('Avg sessions / week', 28)} {_fmt_float(sess_per_week,1)}")
    lines.append(f"{_dotfill('Avg training minutes / week (est)', 28)} {int(round(min_per_week))} min")
    lines.append(f"{_dotfill('Avg sets / session', 28)} {_fmt_float(sets_per_sess,1)}")
    lines.append(f"{_dotfill('Avg weekly tonnage', 28)} {_fmt_int(ton_per_week)} kg")
    lines.append("")
    lines.append("LOAD STRUCTURE")
    lines.append(f"{_dotfill('Hard days (RPE >= 8.5)', 28)} {hard}")
    lines.append(f"{_dotfill('Medium days (7.0-8.4)', 28)} {med}")
    lines.append(f"{_dotfill('Easy days (<7.0)', 28)} {easy}")
    lines.append(f"{_dotfill('Unknown (no RPE)', 28)} {unknown}")
    lines.append(f"{_dotfill('Sessions classified', 28)} {hard + med + easy + unknown} / {sessions}")
    lines.append("")
    lines.append("FATIGUE RESPONSE")
    lines.append(f"{_dotfill('Peak definition', 28)} local maxima (span: 21d)")
    lines.append(f"{_dotfill('N peaks detected', 28)} {peak_count}")
    if peak_count > 0:
        lines.append(f"{_dotfill('Peaks evaluated', 28)} {peaks_evaluated} / {peak_count}")
        lines.append(f"{_dotfill('Peaks excluded', 28)} {peaks_excluded} (insufficient follow-up)")
    if perf_drop is None:
        lines.append(f"{_dotfill('Perf drop after peaks', 28)} n/a (insufficient peaks)")
    else:
        lines.append(f"{_dotfill('Perf drop after peaks', 28)} {perf_drop:.1f}% (median, next 7d, samples: {drop_samples})")
    if rec_days is None:
        lines.append(f"{_dotfill('Recovery time to baseline', 28)} n/a (insufficient peaks)")
    else:
        lines.append(f"{_dotfill('Recovery time to baseline', 28)} {rec_days:.1f} days (median, samples: {rec_samples})")
    lines.append("")
    lines.append("CAPACITY SCORE (internal)")
    lines.append(f"{_dotfill('Work capacity', 28)} {work_capacity} / 100   ({'HIGH' if work_capacity >= 75 else 'MED' if work_capacity >= 55 else 'LOW'})")
    lines.append(f"{_dotfill('Fatigue management', 28)} {fatigue_mgmt} / 100")
    lines.append(f"{_dotfill('Consistency under load', 28)} {consistency} / 100")
    return lines


def _build_capacity_by_muscle_lines(window: Dict[str, Any]) -> List[str]:
    paths = _db_paths()
    p_training = paths["training"]

    muscles = ["Chest", "Triceps", "Biceps", "Lats", "Upper Back", "Shoulders", "Core", "Quads", "Hamstrings", "Calves"]

    def _norm(s: str) -> str:
        s = (s or "").lower()
        out = []
        for ch in s:
            if ch.isalnum() or ch.isspace():
                out.append(ch)
            else:
                out.append(" ")
        return " ".join("".join(out).split())

    # Canonical category -> muscle weights (sum to 1.0)
    cat_w: Dict[str, Dict[str, float]] = {
        "bench": {"Chest": 0.7, "Triceps": 0.3},
        "incline_press": {"Chest": 0.65, "Shoulders": 0.1, "Triceps": 0.25},
        "ohp": {"Shoulders": 0.7, "Triceps": 0.3},
        "dips": {"Chest": 0.4, "Triceps": 0.6},
        "pushup": {"Chest": 0.6, "Triceps": 0.4},
        "fly": {"Chest": 0.9, "Shoulders": 0.1},
        "reverse_fly": {"Shoulders": 0.7, "Upper Back": 0.3},
        "row": {"Upper Back": 0.6, "Lats": 0.4},
        "pulldown": {"Lats": 0.8, "Upper Back": 0.2},
        "pullup": {"Lats": 0.7, "Upper Back": 0.3},
        "curl": {"Biceps": 1.0},
        "triceps_ext": {"Triceps": 1.0},
        "lateral_raise": {"Shoulders": 1.0},
        "y_raise": {"Shoulders": 0.8, "Upper Back": 0.2},
        "squat": {"Quads": 0.6, "Hamstrings": 0.2, "Core": 0.2},
        "leg_press": {"Quads": 0.75, "Hamstrings": 0.15, "Core": 0.10},
        "lunge": {"Quads": 0.6, "Hamstrings": 0.2, "Core": 0.2},
        "leg_extension": {"Quads": 1.0},
        "ham_curl": {"Hamstrings": 1.0},
        "rdl": {"Hamstrings": 0.7, "Core": 0.2, "Upper Back": 0.1},
        "deadlift": {"Hamstrings": 0.45, "Quads": 0.25, "Core": 0.2, "Upper Back": 0.1},
        "calf_raise": {"Calves": 1.0},
        "core": {"Core": 1.0},
        "adductors": {"Quads": 0.5, "Hamstrings": 0.3, "Core": 0.2},
    }

    # safeguard: only explicit calf exercises may contribute to Calves
    for cat, w in cat_w.items():
        if float(w.get("Calves", 0.0) or 0.0) > 1e-9 and cat != "calf_raise":
            raise RuntimeError(f"Invalid Calves attribution in category '{cat}'")

    # Alias substring -> canonical category (case/punct-insensitive; longest wins)
    alias: List[Tuple[str, str]] = [
        ("rear delt fly", "reverse_fly"),
        ("reverse flys", "reverse_fly"),
        ("reverse fly", "reverse_fly"),
        ("reversed flys", "reverse_fly"),
        ("reversed fly", "reverse_fly"),
        ("flyes", "fly"),
        ("flys", "fly"),
        ("cable fly", "fly"),
        ("pec fly", "fly"),
        ("butterfly", "fly"),
        ("brustpresse", "bench"),
        ("rotator cuff", "core"),
        ("rotatoren", "core"),
        ("rotator", "core"),
        ("preacher curls", "curl"),
        ("preachers", "curl"),
        ("carters", "triceps_ext"),
	        ("schraegbankdruecken", "incline_press"),
	        ("schrägbankdrücken", "incline_press"),
	        ("incline press", "incline_press"),
	        ("incline", "incline_press"),
        ("bankdruecken", "bench"),
        ("bankdrücken", "bench"),
        ("bench press", "bench"),
        ("bench", "bench"),
        ("ohp", "ohp"),
        ("overhead press", "ohp"),
        ("military press", "ohp"),
        ("schulterdruecken", "ohp"),
        ("schulterdrücken", "ohp"),
        ("dips", "dips"),
        ("dip", "dips"),
        ("klimmzug", "pullup"),
        ("klimmzüge", "pullup"),
        ("pull up", "pullup"),
        ("pullup", "pullup"),
        ("pull-up", "pullup"),
        ("chin up", "pullup"),
        ("chin-up", "pullup"),
        ("latzug", "pulldown"),
        ("lat pulldown", "pulldown"),
        ("pulldown", "pulldown"),
        ("lat", "pulldown"),
        ("rudern", "row"),
        ("row", "row"),
        ("rows", "row"),
        ("seitheben", "lateral_raise"),
        ("lateral raise", "lateral_raise"),
        ("lateral raises", "lateral_raise"),
        ("y raises", "y_raise"),
        ("y-raises", "y_raise"),
        ("y raises", "y_raise"),
        ("bayesian", "curl"),
        ("bayesians", "curl"),
        ("curl", "curl"),
        ("curls", "curl"),
        ("bizeps", "curl"),
        ("biceps", "curl"),
        ("french press", "triceps_ext"),
        ("trizeps", "triceps_ext"),
        ("triceps", "triceps_ext"),
        ("extension", "triceps_ext"),
        ("beinstrecker", "leg_extension"),
        ("leg extension", "leg_extension"),
        ("kniebeuge", "squat"),
        ("squat", "squat"),
        ("beinpresse", "leg_press"),
        ("leg press", "leg_press"),
        ("ausfallschritt", "lunge"),
        ("lunge", "lunge"),
        ("split squat", "lunge"),
        ("beinbeuger", "ham_curl"),
        ("hamstring curl", "ham_curl"),
        ("rdl", "rdl"),
        ("romanian deadlift", "rdl"),
        ("deadlift", "deadlift"),
        ("wadenheben", "calf_raise"),
        ("calf raise", "calf_raise"),
        ("calf raises", "calf_raise"),
        ("crunch", "core"),
        ("crunches", "core"),
        ("plank", "core"),
        ("adduktor", "adductors"),
        ("adduktoren", "adductors"),
    ]
    alias_norm = sorted([(_norm(a), cat) for a, cat in alias], key=lambda x: len(x[0]), reverse=True)

    def category_for_exercise(name: str, variation: str) -> Optional[str]:
        text = _norm(f"{name} {variation}")
        if not text:
            return None
        for a, cat in alias_norm:
            if a and a in text:
                return cat
        return None

    # Weeks list for CV (include zero weeks for consistency)
    def week_keys_in_window(start: date, end: date) -> List[Tuple[int, int]]:
        keys: List[Tuple[int, int]] = []
        seen = set()
        d = start
        while d <= end:
            iso = d.isocalendar()
            k = (iso.year, iso.week)
            if k not in seen:
                seen.add(k)
                keys.append(k)
            d = date.fromordinal(d.toordinal() + 1)
        return keys

    week_keys = week_keys_in_window(window["start_date"], window["end_date"])
    weeks = float(window["days"]) / 7.0

    total_sets = 0
    mapped_sets = 0
    unmapped_by_ex: Dict[str, int] = {}

    eff_total: Dict[str, float] = {m: 0.0 for m in muscles}
    eff_by_week: Dict[Tuple[str, Tuple[int, int]], float] = {}

    if p_training.exists():
        con = _connect(p_training)
        try:
            rows = con.execute(
                """
                SELECT
                  w.date_iso AS d,
                  e.name AS name,
                  e.variation AS variation
                FROM sets s
                JOIN exercises e ON e.id = s.exercise_id
                JOIN workouts w ON w.id = s.workout_id
                WHERE w.date_iso BETWEEN ? AND ?
                  AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                """,
                (_iso(window["start_date"]), _iso(window["end_date"])),
            ).fetchall()

            for r in rows:
                total_sets += 1
                d = _parse_date_iso_utc(r["d"])
                name = (r["name"] or "").strip()
                variation = (r["variation"] or "").strip()

                cat = category_for_exercise(name, variation)
                if not cat or cat not in cat_w:
                    unmapped_by_ex[name or "UNKNOWN"] = unmapped_by_ex.get(name or "UNKNOWN", 0) + 1
                    continue

                weights = cat_w[cat]
                mapped_sets += 1
                if d:
                    iso = d.isocalendar()
                    wk = (iso.year, iso.week)
                else:
                    wk = None

                for m, wgt in weights.items():
                    if m not in eff_total:
                        continue
                    eff_total[m] += float(wgt)
                    if wk is not None:
                        key = (m, wk)
                        eff_by_week[key] = eff_by_week.get(key, 0.0) + float(wgt)
        finally:
            con.close()

    def weekly_series(m: str) -> List[float]:
        return [float(eff_by_week.get((m, wk), 0.0)) for wk in week_keys]

    def weekly_cv(vals: List[float]) -> Optional[float]:
        if not vals:
            return None
        mean = sum(vals) / len(vals)
        if mean <= 0:
            return None
        var = sum((x - mean) ** 2 for x in vals) / len(vals)
        sd = var ** 0.5
        return (sd / mean) * 100.0

    def level(avg_weekly_eff: float) -> str:
        if avg_weekly_eff < 0.5:
            return "NONE"
        if avg_weekly_eff < 5.0:
            return "LOW"
        if avg_weekly_eff < 10.0:
            return "MED"
        return "HIGH"

    cov_pct = (mapped_sets / total_sets * 100.0) if total_sets > 0 else 0.0
    top_unknown = sorted(unmapped_by_ex.items(), key=lambda x: x[1], reverse=True)[:6]
    top_unknown_s = ", ".join([f"{n} ({c})" for n, c in top_unknown if n]) if top_unknown else "n/a"

    # summary
    avg_weekly: Dict[str, float] = {m: (eff_total[m] / weeks if weeks > 0 else 0.0) for m in muscles}
    top2 = [m for m, _ in sorted(avg_weekly.items(), key=lambda x: x[1], reverse=True)[:2]]
    bottom1 = [m for m, _ in sorted(avg_weekly.items(), key=lambda x: x[1])[:1]]

    lines: List[str] = []
    lines.append(f"CAPACITY BY MUSCLE (SMART SPLIT) — {window['label']}")
    lines.append(_window_line(window))
    lines.append("")
    lines.append(f"{_dotfill('Drop-off definition', 28)} weekly eff-sets CV")
    lines.append(f"{_dotfill('Mapped sets coverage', 28)} {mapped_sets} / {total_sets} ({cov_pct:.1f}%)")
    lines.append(f"{_dotfill('Unmapped sets', 28)} {total_sets - mapped_sets}")
    lines.append(f"{_dotfill('Top unmapped exercises', 28)} {top_unknown_s}")
    lines.append("")

    for m in muscles:
        avgw = avg_weekly[m]
        cv = weekly_cv(weekly_series(m))
        drop_s = "n/a" if cv is None else f"{cv:.1f}%"
        lines.append(f"{_dotfill(m, 28)} {level(avgw):<7} (avg weekly eff sets: {avgw:.1f} | drop-off: {drop_s})")

    lines.append("")
    lines.append(f"{_dotfill('Most trained', 28)} {', '.join(top2) if top2 else 'n/a'}")
    lines.append(f"{_dotfill('Least trained', 28)} {', '.join(bottom1) if bottom1 else 'n/a'}")
    return lines


def _build_compare_avg_assumptions_lines() -> List[str]:
    return [
        "REFERENCE MODEL: AVERAGE (UNTRAINED)",
        "Assumptions:",
        "- Male, 16-40, untrained baseline",
        "- Bench: 60 kg 1RM",
        "- Hinge strength (RDL e1RM): 90 kg x 5 -> e1RM 105 kg",
        "- Pull-ups: 0-3 strict reps (added load baseline +0)",
        "- Weekly training time: 0-60 min",
        "",
        "This is NOT a percentile claim.",
        "This is a practical baseline for context.",
    ]


def _best_strength_set_by_weight(
    con: sqlite3.Connection, patterns: Sequence[str], window: Optional[Dict[str, Any]] = None, min_reps: int = 1
) -> Optional[Dict[str, Any]]:
    wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
    params: List[Any] = [f"%{p.lower()}%" for p in patterns]
    date_sql = ""
    if window is not None:
        date_sql = " AND COALESCE(w.date_iso, w.date) BETWEEN ? AND ?"
        params.extend([_iso(window["start_date"]), _iso(window["end_date"])])
    sql = f"""
      SELECT
        COALESCE(CAST(s.weight AS REAL), 0) AS weight,
        CAST(s.reps AS INTEGER) AS reps,
        COALESCE(w.date_iso, w.date) AS d
      FROM sets s
      JOIN exercises e ON e.id = s.exercise_id
      JOIN workouts w ON w.id = s.workout_id
      WHERE ({wh})
        {date_sql}
        AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) >= ?
      ORDER BY
        COALESCE(CAST(s.weight AS REAL), 0) DESC,
        CAST(s.reps AS INTEGER) DESC,
        s.id DESC
      LIMIT 1
    """
    row = con.execute(sql, (*params, int(min_reps))).fetchone()
    if not row:
        return None
    return {
        "weight": float(row["weight"] or 0.0),
        "reps": int(row["reps"] or 0),
        "date": _fmt_date(_parse_date_iso_utc(row["d"])),
        "e1rm": _epley_1rm(float(row["weight"] or 0.0), int(row["reps"] or 1)),
    }


def _build_compare_avg_lines(window: Dict[str, Any]) -> List[str]:
    paths = _db_paths()
    p_training = paths["training"]

    bench_e1 = None
    rdl_e1 = None
    dips_add = None
    pull_add = None
    bench_sets = 0
    rdl_sets = 0

    strength_totals = {"sessions": 0, "total_sets": 0, "tonnage_kg": 0.0, "est_minutes": 0.0}
    tracked_weeks = 0

    if p_training.exists():
        con = _connect(p_training)
        try:
            strength_totals = _strength_totals(con, window)

            # tracked weeks in window
            rows = con.execute(
                "SELECT DISTINCT substr(date_iso,1,10) AS d FROM workouts WHERE date_iso BETWEEN ? AND ?",
                (_iso(window["start_date"]), _iso(window["end_date"])),
            ).fetchall()
            weeks: set[Tuple[int, int]] = set()
            for r in rows:
                d = _parse_date_iso_utc(r["d"])
                if not d:
                    continue
                iso = d.isocalendar()
                weeks.add((iso.year, iso.week))
            tracked_weeks = len(weeks)

            row = con.execute(
                """
                SELECT COUNT(*) AS n
                FROM sets s
                JOIN exercises e ON e.id = s.exercise_id
                JOIN workouts w ON w.id = s.workout_id
                WHERE w.date_iso BETWEEN ? AND ?
                  AND (LOWER(e.name) LIKE '%bench%' OR LOWER(e.name) LIKE '%bank%')
                  AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                """,
                (_iso(window["start_date"]), _iso(window["end_date"])),
            ).fetchone()
            bench_sets = int(row["n"] or 0)

            row = con.execute(
                """
                SELECT COUNT(*) AS n
                FROM sets s
                JOIN exercises e ON e.id = s.exercise_id
                JOIN workouts w ON w.id = s.workout_id
                WHERE w.date_iso BETWEEN ? AND ?
                  AND (LOWER(e.name) LIKE '%rdl%' OR LOWER(e.name) LIKE '%romanian%')
                  AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                """,
                (_iso(window["start_date"]), _iso(window["end_date"])),
            ).fetchone()
            rdl_sets = int(row["n"] or 0)

            # Use the same selection style as PR top-lifts: heaviest set with reps>=3 (then compute e1RM).
            bench_row = _best_strength_set_by_weight(con, ["bench press", "bench", "bank"], window=window, min_reps=3)
            rdl_row = _best_strength_set_by_weight(con, ["romanian deadlift", "rdl", "romanian"], window=window, min_reps=3)
            bench_e1 = None if not bench_row else float(bench_row["e1rm"] or 0.0)
            rdl_e1 = None if not rdl_row else float(rdl_row["e1rm"] or 0.0)

            def best_added(patterns: Sequence[str]) -> Optional[float]:
                wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
                params = [f"%{p.lower()}%" for p in patterns]
                row = con.execute(
                    f"""
                    SELECT MAX(COALESCE(CAST(s.weight AS REAL),0)) AS w
                    FROM sets s
                    JOIN exercises e ON e.id = s.exercise_id
                    JOIN workouts w ON w.id = s.workout_id
                    WHERE w.date_iso BETWEEN ? AND ?
                      AND ({wh})
                      AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                    """,
                    (_iso(window["start_date"]), _iso(window["end_date"]), *params),
                ).fetchone()
                if not row or row["w"] is None:
                    return None
                v = float(row["w"] or 0.0)
                return v if v > 0 else None

            dips_add = best_added(["dip", "dips"])
            pull_add = best_added(["pull-up", "pull up", "pullups", "chin-up", "chin up", "klimmzug", "klimmzuege"])
        finally:
            con.close()

    def pct_vs(val: Optional[float], base: float) -> str:
        if val is None or base <= 0:
            return "n/a"
        return f"({((float(val) - base) / base) * 100.0:+.0f}%)"

    bench_base = 60.0
    rdl_base = 105.0  # e1RM baseline from 90x5 (Epley)

    sessions = int(strength_totals["sessions"])
    total_sets = int(strength_totals["total_sets"])
    ton = float(strength_totals["tonnage_kg"])
    hours = float(strength_totals["est_minutes"]) / 60.0
    expected = int(((window["days"] + 6) // 7) * 4)
    missed = max(0, expected - sessions)
    miss_rate = (missed / expected * 100.0) if expected > 0 else 0.0

    confidence = "low"
    if sessions >= 12 and (bench_sets + rdl_sets) >= 30:
        confidence = "high"
    elif sessions >= 6:
        confidence = "medium"

    lines: List[str] = []
    lines.append(f"ME VS AVERAGE (UNTRAINED) — {window['label']}")
    lines.append(_window_line(window))
    lines.append(f"{_dotfill('Model', 28)} average (untrained)")
    lines.append(f"{_dotfill('Model confidence', 28)} {confidence}")
    lines.append("")
    lines.append("STRENGTH OUTPUT")
    lines.append(f"{_dotfill('Bench (best e1RM)', 28)} {('n/a' if bench_e1 is None else int(round(bench_e1)))} vs {int(bench_base)}      {pct_vs(bench_e1, bench_base)}")
    lines.append(f"{_dotfill('RDL (best e1RM)', 28)} {('n/a' if rdl_e1 is None else int(round(rdl_e1)))} vs {int(rdl_base)}     {pct_vs(rdl_e1, rdl_base)}")
    dips_me = "n/a" if dips_add is None else f"+{int(round(dips_add))}"
    pull_me = "n/a" if pull_add is None else f"+{int(round(pull_add))}"
    lines.append(f"{_dotfill('Dips (added load best)', 28)} {dips_me} vs +0      (trained skill)")
    lines.append(f"{_dotfill('Pull-ups (added load best)', 28)} {pull_me} vs +0       (trained skill)")
    lines.append("")
    lines.append("CONSISTENCY")
    lines.append(f"{_dotfill('Training sessions', 28)} {sessions}")
    lines.append(f"{_dotfill('Tracked weeks', 28)} {tracked_weeks}")
    lines.append(f"{_dotfill('Expected sessions (4/wk)', 28)} {expected}")
    lines.append(f"{_dotfill('Miss rate', 28)} {miss_rate:.1f}%")
    lines.append("")
    lines.append("WORK OUTPUT")
    lines.append(f"{_dotfill('Total tonnage moved', 28)} {_fmt_int(ton)} kg")
    lines.append(f"{_dotfill('Total training time (est)', 28)} {int(round(hours))}h")
    return lines


def _build_proof_lines(window: Dict[str, Any]) -> List[str]:
    paths = _db_paths()
    p_training = paths["training"]
    p_runs = paths["runs"]
    p_hrv = paths["hrv"]

    sessions = sets = 0
    ton = 0.0
    dist_km = 0.0
    hrv_days = 0

    pr_verified = 0

    expected = int(((window["days"] + 6) // 7) * 4)
    missed = 0
    coverage = None

    if p_training.exists():
        con = _connect(p_training)
        try:
            totals = _strength_totals(con, window)
            sessions = int(totals["sessions"])
            sets = int(totals["total_sets"])
            ton = float(totals["tonnage_kg"])
            cov_min = totals.get("coverage_min")
            cov_max = totals.get("coverage_max")
            if cov_min and cov_max:
                coverage = f"{cov_min} -> {cov_max}"
                try:
                    d0 = date.fromisoformat(str(cov_min))
                    d1 = date.fromisoformat(str(cov_max))
                    span_days = max(1, (d1 - d0).days + 1)
                    expected = int(((span_days + 6) // 7) * 4)
                except Exception:
                    pass

            # verified PRs = key lifts whose all-time best (weight, reps>=3) occurred inside window
            lift_defs_full: Dict[str, Sequence[str]] = {
                "Bench Press": ["bench press", "bench", "bank"],
                "Romanian Deadlift": ["romanian deadlift", "rdl"],
                "Dips": ["dips", "dip"],
                "Pull-Ups": ["pull-up", "pull up", "pullups", "chin-up", "chin up"],
                "Overhead Press": ["overhead press", "ohp", "military press", "shoulder press"],
            }
            for patterns in lift_defs_full.values():
                best = _best_strength_set(con, patterns)
                if not best:
                    continue
                d = date.fromisoformat(best["date"]) if best.get("date") and best["date"] != "n/a" else None
                if d and window["start_date"] <= d <= window["end_date"]:
                    pr_verified += 1
        finally:
            con.close()

    missed = max(0, expected - sessions)

    if p_runs.exists():
        con = _connect(p_runs)
        try:
            dist_km = float(_running_totals(con, window)["distance_km"])
        finally:
            con.close()

    if p_hrv.exists():
        con = _connect(p_hrv)
        try:
            hrv_days = int(_hrv_totals(con, window)["coverage_days"])
        finally:
            con.close()

    lines: List[str] = []
    lines.append("PROOF")
    lines.append(_window_line(window))
    if coverage:
        lines.append(f"{_dotfill('Coverage', 28)} {coverage}")
    lines.append("")
    lines.append(f"{_dotfill('Sessions executed', 28)} {sessions}")
    lines.append(f"{_dotfill('Sets logged', 28)} {sets}")
    lines.append(f"{_dotfill('Tonnage moved', 28)} {_fmt_int(ton)} kg")
    lines.append(f"{_dotfill('PRs (verified)', 28)} {pr_verified}")
    lines.append(f"{_dotfill('Running distance', 28)} {_fmt_float(dist_km,0)} km")
    lines.append(f"{_dotfill('Expected sessions (4/wk)', 28)} {expected}")
    lines.append(f"{_dotfill('Missed sessions (est)', 28)} {missed}")
    lines.append(f"{_dotfill('HRV coverage days', 28)} {hrv_days}")
    lines.append("")
    lines.append("Conclusion:")
    lines.append("This window is output.")
    return lines


def _build_stats_lines(window: Dict[str, Any]) -> List[str]:
    paths = _db_paths()
    p_training = paths["training"]
    p_hrv = paths["hrv"]

    prev = _prev_window(window)

    ton_a = ton_b = 0.0
    sess_a = sess_b = 0

    rmssd_a = rmssd_b = None
    hr_a = hr_b = None

    progressed = None
    progressed_total = None

    if p_training.exists():
        con = _connect(p_training)
        try:
            cur = _strength_totals(con, window)
            prev_tot = _strength_totals(con, prev)
            sess_a = int(cur["sessions"])
            ton_a = float(cur["tonnage_kg"])
            sess_b = int(prev_tot["sessions"])
            ton_b = float(prev_tot["tonnage_kg"])

            # progressed exercises: e1RM best in first half vs second half (only exercises with >=2 appearances)
            start = window["start_date"]
            end = window["end_date"]
            mid = date.fromordinal(start.toordinal() + max(1, (end - start).days // 2))

            rows = con.execute(
                """
                SELECT
                  LOWER(TRIM(e.name)) AS name,
                  SUM(CASE WHEN w.date_iso BETWEEN ? AND ? THEN 1 ELSE 0 END) AS c1,
                  SUM(CASE WHEN w.date_iso BETWEEN ? AND ? THEN 1 ELSE 0 END) AS c2,
                  MAX(CASE WHEN w.date_iso BETWEEN ? AND ? THEN (COALESCE(CAST(s.weight AS REAL),0) * (1.0 + (CAST(s.reps AS REAL)/30.0))) END) AS a,
                  MAX(CASE WHEN w.date_iso BETWEEN ? AND ? THEN (COALESCE(CAST(s.weight AS REAL),0) * (1.0 + (CAST(s.reps AS REAL)/30.0))) END) AS b
                FROM sets s
                JOIN exercises e ON e.id = s.exercise_id
                JOIN workouts w ON w.id = s.workout_id
                WHERE w.date_iso BETWEEN ? AND ?
                  AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
                  AND e.name IS NOT NULL AND TRIM(e.name) != ''
                GROUP BY LOWER(TRIM(e.name))
                """,
                (
                    _iso(start),
                    _iso(mid),
                    _iso(mid),
                    _iso(end),
                    _iso(start),
                    _iso(mid),
                    _iso(mid),
                    _iso(end),
                    _iso(start),
                    _iso(end),
                ),
            ).fetchall()

            prog = 0
            denom = 0
            for r in rows:
                c1 = int(r["c1"] or 0)
                c2 = int(r["c2"] or 0)
                a = _safe_float(r["a"])
                b = _safe_float(r["b"])
                if c1 + c2 < 2:
                    continue
                if a is None or b is None or a <= 0 or b <= 0:
                    continue
                denom += 1
                if b > a * 1.01:
                    prog += 1
            progressed = prog
            progressed_total = denom
        finally:
            con.close()

    if p_hrv.exists():
        con = _connect(p_hrv)
        try:
            def avg(col: str, w: Dict[str, Any]) -> Optional[float]:
                row = con.execute(
                    f"""
                    SELECT AVG({col}) AS m
                    FROM hrv_measurements
                    WHERE substr(date_utc,1,10) BETWEEN ? AND ?
                      AND {col} IS NOT NULL AND {col} > 0
                    """,
                    (_iso(w["start_date"]), _iso(w["end_date"])),
                ).fetchone()
                return None if not row or row["m"] is None else float(row["m"])

            rmssd_a = avg("rmssd", window)
            rmssd_b = avg("rmssd", prev)
            hr_a = avg("hr", window)
            hr_b = avg("hr", prev)
        finally:
            con.close()

    ton_pct = _pct_change(ton_a, ton_b)
    sess_pct = _pct_change(sess_a, sess_b)
    rmssd_delta = None if (rmssd_a is None or rmssd_b is None) else (rmssd_a - rmssd_b)
    hr_delta = None if (hr_a is None or hr_b is None) else (hr_a - hr_b)

    lines: List[str] = []
    lines.append("STATS (quick)")
    lines.append(_window_line(window))
    lines.append(f"{_dotfill('Δ vs previous window', 28)} {prev['label']}")
    lines.append("")
    lines.append(f"{_dotfill('Tonnage', 28)} {_fmt_int(ton_a)} kg  ({ton_pct})")
    lines.append(f"{_dotfill('Sessions', 28)} {sess_a}          ({sess_pct})")
    lines.append(
        f"{_dotfill('RMSSD baseline', 28)} {('n/a' if rmssd_a is None else str(int(round(rmssd_a))))} ms     ({'n/a' if rmssd_delta is None else f'{int(round(rmssd_delta)):+d}'})"
    )
    lines.append(
        f"{_dotfill('Resting HR', 28)} {('n/a' if hr_a is None else str(int(round(hr_a))))} bpm     ({'n/a' if hr_delta is None else f'{int(round(hr_delta)):+d}'})"
    )
    if progressed_total is None or progressed_total == 0:
        lines.append(f"{_dotfill('Progressed exercises', 28)} n/a (insufficient history)")
    else:
        lines.append(f"{_dotfill('Progressed exercises', 28)} {progressed} / {progressed_total}")
    return lines


def _tag_recent_strength_entries(entries: List[Dict[str, Any]], strength_meta: Dict[str, Dict[str, Any]]) -> None:
    """
    Mutates entries in-place by assigning `line` for kind=='strength'.
    Label taxonomy matches pr --recent:
      (all-time PR) / (window peak) / (tied) / (rep PR) / (repeat) / (single)
    Window peak / tied / repeat are based on e1RM score.
    """
    for lift in {e["lift"] for e in entries if e.get("kind") == "strength"}:
        idxs = [i for i, e in enumerate(entries) if e.get("kind") == "strength" and e.get("lift") == lift]
        if not idxs:
            continue
        best_w = float(strength_meta.get(lift, {}).get("best_w") or 0.0)
        near_tol = float(strength_meta.get(lift, {}).get("near_tol") or 2.5)
        near_reps = int(strength_meta.get(lift, {}).get("near_reps") or 0)

        best_in_entries = max(
            (float(entries[i].get("e1rm") or 0.0) for i in idxs if int(entries[i].get("reps") or 0) >= 3),
            default=0.0,
        )

        seen_all = False
        seen_peak = False
        seen_rep = False

        for i in idxs:  # idxs follow entries order (date desc)
            e = entries[i]
            w = float(e.get("weight") or 0.0)
            reps = int(e.get("reps") or 0)
            tag = "(repeat)"
            if reps == 1:
                tag = "(single)"
            else:
                if best_w > 0 and abs(w - best_w) <= 1e-9:
                    if not seen_all:
                        tag = "(all-time PR)"
                        seen_all = True
                    else:
                        tag = "(tied)"
                elif best_in_entries > 0 and abs(float(e.get("e1rm") or 0.0) - best_in_entries) <= 1e-9:
                    if not seen_peak:
                        tag = "(window peak)"
                        seen_peak = True
                    else:
                        tag = "(tied)"
                elif best_w > 0 and (w >= (best_w - near_tol)) and near_reps > 0 and reps >= near_reps:
                    if not seen_rep:
                        tag = "(rep PR)"
                        seen_rep = True
            entries[i]["line"] = f"{entries[i]['display_base']} {tag}"


def _build_recent_peaks_lines(n: int) -> List[str]:
    n = max(1, min(50, int(n)))

    db_dir = _resolve_db_dir()
    roles = dict(DB_ROLES_DEFAULT)
    p_training = db_dir / roles["training"]
    p_runs = db_dir / roles["runs"]
    p_hrv = db_dir / roles["hrv"]

    events: List[Dict[str, Any]] = []
    strength_meta: Dict[str, Dict[str, Any]] = {}

    def add_line(d: date, line: str) -> None:
        events.append({"date": d, "kind": "line", "line": line})

    # ---- Recovery: records
    if p_hrv.exists():
        con = _connect(p_hrv)
        try:
            cols = set(_columns(con, "hrv_measurements"))
            dcol = "date_utc" if "date_utc" in cols else None
            if dcol and "rmssd" in cols:
                row = con.execute(
                    f"""
                    SELECT {dcol} AS d, rmssd AS v
                    FROM hrv_measurements
                    WHERE rmssd IS NOT NULL AND rmssd > 0
                    ORDER BY rmssd DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    d = _parse_date_iso_utc(row["d"])
                    if d:
                        add_line(d, f"{_dotfill('RMSSD', 22)} {int(round(float(row['v'])))} ms   (record)")

            if dcol and "hr" in cols:
                row = con.execute(
                    f"""
                    SELECT {dcol} AS d, hr AS v
                    FROM hrv_measurements
                    WHERE hr IS NOT NULL AND hr > 0
                    ORDER BY hr ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    d = _parse_date_iso_utc(row["d"])
                    if d:
                        add_line(d, f"{_dotfill('Resting HR', 22)} {int(round(float(row['v'])))} bpm     (record)")
        finally:
            con.close()

    # ---- Running: season high + near PR
    if p_runs.exists():
        con = _connect(p_runs)
        try:
            cols = set(_columns(con, "runs"))
            if {"date", "distance", "moving_time"}.issubset(cols):
                rows = con.execute(
                    """
                    SELECT date AS d, distance AS dist, moving_time AS t, max_hr AS mx
                    FROM runs
                    WHERE distance IS NOT NULL AND distance > 0
                    ORDER BY date DESC
                    LIMIT 400
                    """
                ).fetchall()

                pts: List[Tuple[date, float, int]] = []
                max_bpm: Optional[Tuple[date, int]] = None
                for r in rows:
                    d = _parse_date_iso_utc(r["d"])
                    if not d:
                        continue
                    dist_m = float(r["dist"] or 0.0)
                    sec = int(r["t"] or 0)
                    pts.append((d, dist_m, sec))
                    if r["mx"] is not None:
                        bpm = int(round(float(r["mx"])))
                        if bpm > 0 and (max_bpm is None or bpm > max_bpm[1]):
                            max_bpm = (d, bpm)

                # Highest BPM (record)
                if max_bpm:
                    d, bpm = max_bpm
                    # If it's all-time max we just fetched from recent window it might miss older records.
                    # Ensure true all-time:
                    row = con.execute(
                        "SELECT date AS d, MAX(max_hr) AS mx FROM runs WHERE max_hr IS NOT NULL AND max_hr > 0"
                    ).fetchone()
                    if row and row["mx"] is not None:
                            d2 = _parse_date_iso_utc(row["d"])
                            if d2:
                                add_line(d2, f"{_dotfill('Highest BPM', 22)} {int(round(float(row['mx'])))} bpm  (record)")

                # Season high (current year): longest distance so far in that year
                pts.sort(key=lambda x: x[0])
                best_year: Dict[int, float] = {}
                season_events: List[Tuple[date, float]] = []
                for d, dist_m, _ in pts:
                    y = d.year
                    km = dist_m / 1000.0
                    prev = best_year.get(y, 0.0)
                    if km > prev + 1e-9:
                        best_year[y] = km
                        season_events.append((d, km))
                    if season_events:
                        d, km = season_events[-1]
                        add_line(d, f"{_dotfill('Long run', 22)} {km:.1f} km    (season high)")

                # Near PR 5k: within 5% of all-time best 5k time
                best_5k_row = con.execute(
                    """
                    SELECT date AS d, distance AS dist, moving_time AS t
                    FROM runs
                    WHERE distance BETWEEN 4700 AND 5300
                      AND moving_time IS NOT NULL AND moving_time > 0
                    ORDER BY moving_time ASC
                    LIMIT 1
                    """
                ).fetchone()
                best_5k_sec = int(best_5k_row["t"]) if best_5k_row else 0
                recent_5k_row = con.execute(
                    """
                    SELECT date AS d, distance AS dist, moving_time AS t
                    FROM runs
                    WHERE distance BETWEEN 4700 AND 5300
                      AND moving_time IS NOT NULL AND moving_time > 0
                    ORDER BY date DESC
                    LIMIT 30
                    """
                ).fetchall()
                for r in recent_5k_row:
                    d = _parse_date_iso_utc(r["d"])
                    if not d:
                        continue
                    sec = int(r["t"] or 0)
                    if best_5k_sec > 0 and sec > 0 and sec <= int(round(best_5k_sec * 1.05)):
                        add_line(d, f"{_dotfill('5 km effort', 22)} {_sec_to_hms(sec)}      (near PR)")
                        break
        finally:
            con.close()

    # ---- Strength: recent notable sets per lift
    if p_training.exists():
        con = _connect(p_training)
        try:
            lift_info = [
                entry for entry in PR_STRENGTH_LIFT_DEFS if entry.get("recent_include", True) is not False
            ]

            for entry in lift_info:
                lift = entry["label"]
                patterns = entry["patterns"]
                recent = _recent_strength_events(
                    con, patterns, limit_rows=260, exclude_patterns=entry.get("exclude")
                )
                if not recent:
                    continue

                near_tol = entry.get("near_tol", 2.5)
                min_reps = int(entry.get("min_reps", 3))
                all_time_best_weight = _all_time_best_weight(con, patterns, min_reps=min_reps)
                near_best_reps = _all_time_near_best_max_reps(
                    con, patterns, all_time_best_weight, near_tol, min_reps=min_reps
                )
                strength_meta[lift] = {"best_w": all_time_best_weight, "near_tol": near_tol, "near_reps": near_best_reps}

                seen_lift_date: set[Tuple[str, date]] = set()
                for r in recent[:80]:
                    d = r["date"]
                    w = float(r["weight"] or 0.0)
                    reps = int(r["reps"] or 0)
                    if w <= 0:
                        continue
                    if reps == 2:
                        continue
                    if reps != 1 and reps < min_reps:
                        continue
                    if (lift, d) in seen_lift_date:
                        continue

                    display_name = r.get("exercise_display") or lift
                    if entry.get("bodyweight"):
                        main = f"+{w:.1f} x {reps}" if w > 0 else f"BW x {reps}"
                    else:
                        main = f"{w:.1f} x {reps}"

                    seen_lift_date.add((lift, d))
                    label = _dotfill(display_name, 22)
                    events.append(
                        {
                            "date": d,
                            "kind": "strength",
                            "lift": lift,
                            "weight": w,
                            "reps": reps,
                            "e1rm": _epley_1rm(w, reps if reps > 0 else 1),
                            "display_base": f"{label} {main:<10}",
                            "exercise_display": display_name,
                        }
                    )
        finally:
            con.close()

    # ---- Sort + take most recent N unique entries by date desc
    events.sort(key=lambda x: x["date"], reverse=True)
    picked: List[Dict[str, Any]] = []
    used: set[Tuple[Any, ...]] = set()
    for e in events:
        if e.get("kind") == "strength":
            key = (e["date"], e["lift"], round(float(e["weight"] or 0.0), 2), int(e["reps"] or 0))
        else:
            key = (e["date"], e["line"])
        if key in used:
            continue
        used.add(key)
        picked.append(e)
        if len(picked) >= n:
            break

    _tag_recent_strength_entries(picked, strength_meta)

    out: List[str] = []
    out.append(f"RECENT PEAKS (last {n})")
    out.append("")
    for e in picked:
        out.append(f"{e['date'].isoformat()}  {e['line']}")
    return out


def _strength_pr_quality(con: sqlite3.Connection, lift_defs: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Returns (retention_percent, pr_count_90d).
    - retention_percent: mean of best_60d/all_time per lift.
    - pr_count_90d: count of new all-time bests in last 90 days across lifts.
    """
    today = date.today()
    d60 = today.toordinal() - 60
    d90 = today.toordinal() - 90

    ratios: List[float] = []
    pr_events_90d = 0

    for entry in lift_defs:
        patterns = entry["patterns"]
        wh = " OR ".join(["LOWER(e.name) LIKE ?" for _ in patterns])
        params = [f"%{p.lower()}%" for p in patterns]
        rows = con.execute(
            f"""
            SELECT
              COALESCE(w.date_iso, w.date) AS d,
              MAX(COALESCE(CAST(s.weight AS REAL),0) * (1.0 + (CAST(s.reps AS REAL)/30.0))) AS e1rm
            FROM sets s
            JOIN exercises e ON e.id = s.exercise_id
            JOIN workouts w ON w.id = s.workout_id
            WHERE ({wh})
              AND s.reps IS NOT NULL AND CAST(s.reps AS INTEGER) > 0
            GROUP BY COALESCE(w.date_iso, w.date)
            """,
            params,
        ).fetchall()

        pts: List[Tuple[int, float]] = []
        for r in rows:
            d = _parse_date_iso_utc(r["d"])
            e1 = _safe_float(r["e1rm"])
            if not d or e1 is None:
                continue
            pts.append((d.toordinal(), float(e1)))

        if not pts:
            continue

        pts.sort(key=lambda x: x[0])
        all_time = max(v for _, v in pts)
        if all_time > 0:
            best_60 = max((v for dd, v in pts if dd >= d60), default=0.0)
            ratios.append(best_60 / all_time if best_60 > 0 else 0.0)

        best_so_far = 0.0
        for dd, v in pts:
            if v > best_so_far + 1e-9:
                best_so_far = v
                if dd >= d90:
                    pr_events_90d += 1

    retention = int(round((sum(ratios) / len(ratios)) * 100.0)) if ratios else 0
    return retention, pr_events_90d


def _pr_frequency_90d(
    lift_defs: Sequence[Dict[str, Any]], p_training: Path, p_runs: Path, p_hrv: Path
) -> int:
    """
    Count events in the last 90 days that would be tagged as:
      (all-time PR) OR (record)
    using the same detection rules as pr --recent.
    """
    since = date.today() - timedelta(days=89)
    cnt = 0

    # Strength: build a 90d event list and tag with the same logic as pr --recent.
    if p_training.exists():
        con = _connect(p_training)
        try:
            entries: List[Dict[str, Any]] = []
            meta: Dict[str, Dict[str, Any]] = {}
            for entry in lift_defs:
                lift = entry["label"]
                patterns = entry["patterns"]
                near_tol = entry.get("near_tol", 2.5)
                min_reps = int(entry.get("min_reps", 3))
                best_w = _all_time_best_weight(con, patterns, min_reps=min_reps)
                near_reps = _all_time_near_best_max_reps(
                    con, patterns, best_w, near_tol, min_reps=min_reps
                )
                meta[lift] = {"best_w": best_w, "near_tol": near_tol, "near_reps": near_reps}

                recent = _recent_strength_events(
                    con,
                    patterns,
                    limit_rows=3000,
                    exclude_patterns=entry.get("exclude"),
                )
                seen_date: set[date] = set()
                for r in recent:
                    d = r["date"]
                    if d < since:
                        break
                    if d in seen_date:
                        continue
                    seen_date.add(d)
                    w = float(r.get("weight") or 0.0)
                    reps = int(r.get("reps") or 0)
                    if w <= 0 or reps <= 0:
                        continue
                    if reps == 2:
                        continue
                    if reps != 1 and reps < min_reps:
                        continue

                    display_name = r.get("exercise_display") or lift
                    if lift in {"Dips", "Pull-Ups"}:
                        main = f"+{w:.1f} x {reps}"
                    else:
                        main = f"{w:.1f} x {reps}"
                    label = _dotfill(display_name, 22)

                    entries.append(
                        {
                            "date": d,
                            "kind": "strength",
                            "lift": lift,
                            "weight": w,
                            "reps": reps,
                            "e1rm": _epley_1rm(w, reps),
                            "display_base": f"{label} {main:<10}",
                            "exercise_display": display_name,
                        }
                    )

            entries.sort(key=lambda x: x["date"], reverse=True)
            _tag_recent_strength_entries(entries, meta)
            cnt += sum(1 for e in entries if e.get("kind") == "strength" and "(all-time PR)" in str(e.get("line") or ""))
        finally:
            con.close()

    # Recovery records
    if p_hrv.exists():
        con = _connect(p_hrv)
        try:
            cols = set(_columns(con, "hrv_measurements"))
            dcol = "date_utc" if "date_utc" in cols else None
            if dcol and "rmssd" in cols:
                row = con.execute(
                    f"""
                    SELECT {dcol} AS d
                    FROM hrv_measurements
                    WHERE rmssd IS NOT NULL AND rmssd > 0
                    ORDER BY rmssd DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    d = _parse_date_iso_utc(row["d"])
                    if d and d >= since:
                        cnt += 1
            if dcol and "hr" in cols:
                row = con.execute(
                    f"""
                    SELECT {dcol} AS d
                    FROM hrv_measurements
                    WHERE hr IS NOT NULL AND hr > 0
                    ORDER BY hr ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    d = _parse_date_iso_utc(row["d"])
                    if d and d >= since:
                        cnt += 1
        finally:
            con.close()

    # Running record: Highest BPM (max_hr)
    if p_runs.exists():
        con = _connect(p_runs)
        try:
            cols = set(_columns(con, "runs"))
            if "max_hr" in cols and "date" in cols:
                row = con.execute(
                    """
                    SELECT date AS d
                    FROM runs
                    WHERE max_hr IS NOT NULL AND max_hr > 0
                    ORDER BY max_hr DESC, date DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    d = _parse_date_iso_utc(row["d"])
                    if d and d >= since:
                        cnt += 1
        finally:
            con.close()

    return cnt


def _about_report_lines() -> List[str]:
    stats = _build_about_stats()
    lines: List[str] = []

    def add_field(label: str, value: Any) -> None:
        lines.append(f"{_dotfill(label, 23)} {value}")

    def add_wrapped(label: str, first: str, rest: Sequence[str]) -> None:
        add_field(label, first)
        indent = " " * 25
        for item in rest:
            lines.append(f"{indent}{item}")

    def status_from_bool(ok: bool, yes: str = "active", no: str = "inactive") -> str:
        return yes if ok else no

    db_dir = _resolve_db_dir()
    roles = dict(DB_ROLES_DEFAULT)
    db_paths = {k: (db_dir / v) for k, v in roles.items()}
    has_training = db_paths["training"].exists()
    has_hrv = db_paths["hrv"].exists()
    has_nutrition = db_paths["ernaehrung"].exists()
    has_runs = db_paths["runs"].exists()
    has_plans = db_paths["plans"].exists()
    memory_root = BASE_DIR.parent / "data" / "memory"
    has_memory = memory_root.exists()

    backup_status = str(stats.get("backup_status", "n/a") or "n/a")
    integrity = str(stats.get("data_integrity", "n/a") or "n/a")
    integrity_line = "verified with warnings" if integrity == "verified" else integrity

    lines.append("")
    lines.append("LIVA — SYSTEM OVERVIEW")
    add_field("Owner", stats.get("owner", "n/a"))
    add_field("System", "LIVA / LIVA")
    add_field("Build", stats.get("build", "n/a"))
    add_field("Host", "LIVA mini-PC")
    add_field("Runtime", "Flask / Python / SQLite")
    add_field("Interface", "Web UI, fake CMD, GPT Actions, Telegram")
    add_field("Codebase", f"{_fmt_int(stats.get('codebase_lines'))} lines")
    add_field("Stored values", _fmt_int(stats.get("stored_values")))
    add_field("Active profiles", stats.get("active_profiles", "n/a"))
    add_field("Data integrity", integrity_line)
    lines.append("")
    lines.append("MISSION")
    add_field("Purpose", "personal coaching + tracking system")
    add_field("Primary role", "training decisions, progression, recovery, planning")
    add_field("Design principle", "own data > generic fitness app")
    add_field("Decision layer", "LIVA / CORE")
    add_field("Coach mode", "data-first, context-aware, explainable")
    lines.append("")
    lines.append("TRACKED DOMAINS")
    lines.append("")
    add_wrapped("Strength Training", "workouts, sessions, exercises, sets, reps, weight, RPE", [
        "tonnage, volume, top sets, backoff sets, e1RM, PRs",
        "progression, exercise history, device variants",
    ])
    lines.append("")
    add_wrapped("Exercise Mapping", "canonical exercise names, aliases, device types", [
        "muscle groups, movement patterns, unmapped sets",
        "duplicate names, stale exercises",
    ])
    lines.append("")
    add_wrapped("Progression", "suggested loads, previous performance, rep ranges", [
        "progression attempts, repeats, holds, deload signals",
        "exercise-specific trends",
    ])
    lines.append("")
    add_wrapped("Muscle Balance", "chest, lats, upper back, shoulders, biceps, triceps", [
        "quads, hamstrings, calves, core",
        "weekly effective sets, underdosed areas, drop-off",
    ])
    lines.append("")
    add_wrapped("Running", "runs, distance, duration, pace, avg bpm, max bpm", [
        "longest run, best 5k, best 10k, season highs",
    ])
    lines.append("")
    add_wrapped("Ergo / Cardio", "ergo sessions, duration, watt, cadence, calculated distance", [
        "zone distribution, cardio load, weekly volume",
    ])
    lines.append("")
    add_wrapped("Recovery", "RMSSD, SDNN, AVNN, resting bpm, HRV baseline", [
        "readiness, recovery trend, fatigue flags",
    ])
    lines.append("")
    add_wrapped("Sleep / Daily State", "sleep quality, wakeup time, wakeup variance", [
        "motivation, sickness, stress notes, daily form",
    ])
    lines.append("")
    add_wrapped("Body Metrics", "bodyweight, weight trend, trend direction", [
        "cut/bulk context, maintenance estimate",
    ])
    lines.append("")
    add_wrapped("Nutrition", "calories, protein, carbs, fats, sugar", [
        "meals, meal timing, meal reminders, maintenance",
        "tracking freshness, macro consistency",
    ])
    lines.append("")
    add_wrapped("Planning", "training plans, active blocks, planned sessions", [
        "missed sessions, streaks, start dates, overrides",
        "calendar conflicts, next likely session",
    ])
    lines.append("")
    add_wrapped("CORE / LIVA", "daily mode, Heavy/Normal/Light decisions", [
        "overrides, sidecar state, objections, confidence",
        "readiness logic, training recommendation",
    ])
    lines.append("")
    add_wrapped("Calendar", "Google Calendar sync, training events", [
        "time windows, conflicts, planned training slots",
        "extended event metadata",
    ])
    lines.append("")
    add_wrapped("Telegram", "meal reminders, system messages", [
        "reminder timing, notification status",
    ])
    lines.append("")
    add_wrapped("Memory / Dossier", "athlete profile, long-term notes, training identity", [
        "preferences, rules, context, system assumptions",
    ])
    lines.append("")
    add_wrapped("Remote / Smart Home", "PC status, monitor status, wake-on-LAN", [
        "smart plugs, lights, late-PC automation",
        "remote dashboard state",
    ])
    lines.append("")
    add_wrapped("Dashboards", "training dashboard, cardio dashboard, CORE board", [
        "memory view, remote view, morning check-in boards",
    ])
    lines.append("")
    add_field("Exports", "CSV, JSON, internal reports")
    add_field("Backups", "database backups, local snapshots")
    add_field("Actions API", "LIVA Actions V2, read endpoints, write commands")
    add_field("Offline status", "ready, no external dependency for core data")
    lines.append("")
    lines.append("DATA SOURCES")
    add_field("Primary database", "SQLite")
    add_field("Training DB", status_from_bool(has_training))
    add_field("CORE DB", status_from_bool(has_training))
    add_field("HRV DB", status_from_bool(has_hrv))
    add_field("Nutrition DB", status_from_bool(has_nutrition))
    add_field("Runs/Cardio DB", status_from_bool(has_runs))
    add_field("Plans DB", status_from_bool(has_plans))
    add_field("Memory DB", "active" if has_memory else "file-backed")
    lines.append("")
    lines.append("CONNECTED SYSTEMS")
    add_field("Google Calendar", "connected")
    add_field("Telegram Hub", "connected")
    add_field("GPT Actions", "connected")
    add_field("Tailscale", "available")
    add_field("Smart Home Layer", "partial / local")
    add_field("Export Layer", "available")
    lines.append("")
    lines.append("DATA QUALITY")
    add_field("First record", stats.get("first_record", "n/a"))
    add_field("Last record", stats.get("last_record", "n/a"))
    add_field("Tracked since", stats.get("tracked_since", "n/a"))
    add_field("HRV coverage", "active" if has_hrv else "inactive")
    add_field("Training coverage", "active" if has_training else "inactive")
    add_field("Nutrition coverage", "partial" if has_nutrition else "inactive")
    add_field("Cardio coverage", "active" if has_runs else "inactive")
    add_field("Bodyweight coverage", "active" if has_nutrition else "inactive")
    lines.append("")
    add_wrapped("Known warnings", "exercise aliases need cleanup", [
        "duplicate PR entries possible",
        "unmapped muscle sets exist",
        "hard-day classifier too broad",
    ])
    lines.append("")
    lines.append("SYSTEM STATUS")
    add_field("LIVA", "online")
    add_field("LIVA Coach", "active")
    add_field("CORE engine", "active")
    add_field("Database freshness", "fresh")
    add_field("Backup status", backup_status)
    add_field("Backup last", stats.get("backup_last", "n/a"))
    add_field("Export mode", stats.get("export_mode", "n/a"))
    add_field("Offline mode", "ready")
    lines.append("")
    lines.append("CURRENT CHAPTER")
    lines.append("From tracking system to decision system.")
    lines.append("")
    add_field("Generated at", stats.get("generated_at", "n/a"))
    return lines


HELP_COMMAND_DEFS: List[Tuple[str, str]] = [
    ("about | about.exe", "Show system overview"),
    ("pr", "Show verified peaks (all-time)"),
    ("pr --recent N", "Show recent peaks (last N)"),
    ("progress --compare N", "Compare last N days vs previous N"),
    ("capacity [--days N | --since YYYY-MM-DD]", ""),
    ("capacity --by muscle [--days N | --since YYYY-MM-DD]", ""),
    ("compare avg --assumptions", ""),
    ("compare avg [--days N | --since YYYY-MM-DD]", ""),
    ("proof --alltime | proof [--days N | --since YYYY-MM-DD]", ""),
    ("stats [--days N | --since YYYY-MM-DD]", ""),
    ("cls | clear", "Clear screen"),
    ("restart | sudo systemctl restart liva.service", "Restart terminal"),
    ("exit", "Close window"),
    ("debug | diag", "Run diagnostic report (sequence of commands)"),
]


def _build_help_lines() -> List[str]:
    lines = []
    for command, description in HELP_COMMAND_DEFS:
        if description:
            lines.append(f"  {command}   {description}")
        else:
            lines.append(f"  {command}")
    return lines


def _build_pr_lines() -> List[str]:
    db_dir = _resolve_db_dir()
    roles = dict(DB_ROLES_DEFAULT)
    p_training = db_dir / roles["training"]
    p_runs = db_dir / roles["runs"]
    p_hrv = db_dir / roles["hrv"]

    strength_rows: Dict[str, Optional[Dict[str, Any]]] = {}
    retention_60 = 0
    if p_training.exists():
        con = _connect(p_training)
        try:
            for entry in PR_STRENGTH_LIFT_DEFS:
                name = entry["label"]
                strength_rows[name] = _best_strength_set(
                    con,
                    entry["patterns"],
                    min_reps=int(entry.get("min_reps", 3)),
                    exclude_patterns=entry.get("exclude"),
                )
            retention_60, _ = _strength_pr_quality(con, PR_STRENGTH_LIFT_DEFS)
        finally:
            con.close()
    pr_freq_90 = _pr_frequency_90d(PR_STRENGTH_LIFT_DEFS, p_training, p_runs, p_hrv)

    # Running
    best_5k = None
    best_10k = None
    longest = None
    highest_bpm = None
    if p_runs.exists():
        con = _connect(p_runs)
        try:
            cols = set(_columns(con, "runs"))
            date_col = "date" if "date" in cols else None
            dist_col = "distance" if "distance" in cols else None
            time_col = "moving_time" if "moving_time" in cols else None
            max_hr_col = "max_hr" if "max_hr" in cols else None
            if date_col and dist_col and time_col:
                best_5k = con.execute(
                    f"""
                    SELECT {date_col} AS d, {dist_col} AS dist, {time_col} AS t
                    FROM runs
                    WHERE {dist_col} BETWEEN 4700 AND 5300
                      AND {time_col} IS NOT NULL AND {time_col} > 0
                    ORDER BY {time_col} ASC
                    LIMIT 1
                    """
                ).fetchone()
                best_10k = con.execute(
                    f"""
                    SELECT {date_col} AS d, {dist_col} AS dist, {time_col} AS t
                    FROM runs
                    WHERE {dist_col} BETWEEN 9700 AND 10300
                      AND {time_col} IS NOT NULL AND {time_col} > 0
                    ORDER BY {time_col} ASC
                    LIMIT 1
                    """
                ).fetchone()
                longest = con.execute(
                    f"""
                    SELECT {date_col} AS d, {dist_col} AS dist, {time_col} AS t
                    FROM runs
                    WHERE {dist_col} IS NOT NULL AND {dist_col} > 0
                    ORDER BY {dist_col} DESC, {time_col} DESC
                    LIMIT 1
                    """
                ).fetchone()
            if max_hr_col:
                row = con.execute(
                    f"SELECT {date_col or 'date'} AS d, MAX({max_hr_col}) AS mx FROM runs WHERE {max_hr_col} IS NOT NULL"
                ).fetchone()
                if row and row["mx"] is not None:
                    highest_bpm = {"bpm": int(round(float(row["mx"]))), "date": _fmt_date(_parse_date_iso_utc(row["d"]))}
        finally:
            con.close()

    # Recovery
    highest_rmssd = None
    lowest_hr = None
    if p_hrv.exists():
        con = _connect(p_hrv)
        try:
            cols = set(_columns(con, "hrv_measurements"))
            dcol = "date_utc" if "date_utc" in cols else None
            rmssd_col = "rmssd" if "rmssd" in cols else None
            hr_col = "hr" if "hr" in cols else None
            if dcol and rmssd_col:
                row = con.execute(
                    f"""
                    SELECT {dcol} AS d, {rmssd_col} AS v
                    FROM hrv_measurements
                    WHERE {rmssd_col} IS NOT NULL AND {rmssd_col} > 0
                    ORDER BY {rmssd_col} DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    highest_rmssd = {"rmssd": int(round(float(row["v"]))), "date": _fmt_date(_parse_date_iso_utc(row["d"]))}
            if dcol and hr_col:
                row = con.execute(
                    f"""
                    SELECT {dcol} AS d, {hr_col} AS v
                    FROM hrv_measurements
                    WHERE {hr_col} IS NOT NULL AND {hr_col} > 0
                    ORDER BY {hr_col} ASC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    lowest_hr = {"bpm": int(round(float(row["v"]))), "date": _fmt_date(_parse_date_iso_utc(row["d"]))}
        finally:
            con.close()

    lines: List[str] = []
    lines.append("PR — VERIFIED PEAKS (ALL-TIME)")
    lines.append("")
    lines.append("STRENGTH (top lifts)")

    def _fmt_strength(entry: Dict[str, Any], row: Optional[Dict[str, Any]]) -> str:
        if not row:
            return f"{_dotfill(entry['label'], 28)} n/a"

        w = float(row["weight"] or 0.0)
        reps = int(row["reps"] or 1)
        e1 = float(row["e1rm"] or 0.0)
        d = row["date"]
        display_name = row.get("exercise_display") or entry["label"]

        if entry.get("bodyweight"):
            if w > 0:
                main = f"BW +{w:.1f} kg x {reps}"
            else:
                main = f"BW x {reps}"
            est = f"(est 1RM add: +{e1:.1f})"
            return f"{_dotfill(display_name, 28)} {main:<20} {est:<20} date: {d}"

        if reps == 1:
            main = f"{w:.1f} kg"
        else:
            main = f"{w:.1f} kg x {reps}"
        return f"{_dotfill(display_name, 28)} {main:<20} (est 1RM: {e1:.1f})  date: {d}"

    for entry in PR_STRENGTH_LIFT_DEFS:
        lines.append(_fmt_strength(entry, strength_rows.get(entry["label"])))

    lines.append("")
    lines.append("RUNNING (best efforts)")

    def _fmt_run(label: str, r: Any, kind: str) -> str:
        if not r:
            return f"{_dotfill(label, 28)} n/a"
        dist_m = float(r["dist"] or 0.0)
        sec = int(r["t"] or 0)
        d = _fmt_date(_parse_date_iso_utc(r["d"]))
        km = dist_m / 1000.0 if dist_m > 0 else 0.0
        if kind == "long":
            return f"{_dotfill(label, 28)} {km:.1f} km    {_sec_to_hms(sec)}          date: {d}"
        pace = _pace_str(sec, km if km > 0 else (5.0 if kind == '5k' else 10.0))
        return f"{_dotfill(label, 28)} {_sec_to_hms(sec)}   pace: {pace}   date: {d}"

    lines.append(_fmt_run("5 km", best_5k, "5k"))
    lines.append(_fmt_run("10 km", best_10k, "10k"))
    lines.append(_fmt_run("Longest run", longest, "long"))

    lines.append("")
    lines.append("RECOVERY (records)")
    if highest_rmssd:
        lines.append(f"{_dotfill('Highest RMSSD', 28)} {highest_rmssd['rmssd']} ms   date: {highest_rmssd['date']}")
    else:
        lines.append(f"{_dotfill('Highest RMSSD', 28)} n/a")
    if lowest_hr:
        lines.append(f"{_dotfill('Lowest resting HR', 28)} {lowest_hr['bpm']} bpm   date: {lowest_hr['date']}")
    else:
        lines.append(f"{_dotfill('Lowest resting HR', 28)} n/a")
    if highest_bpm:
        lines.append(f"{_dotfill('Highest BPM', 28)} {highest_bpm['bpm']} bpm  date: {highest_bpm['date']}")
    else:
        lines.append(f"{_dotfill('Highest BPM', 28)} n/a")

    lines.append("")
    lines.append("PR QUALITY")
    lines.append(f"{_dotfill('Peak-to-baseline retention', 28)} {retention_60}% (last 60d)")
    lines.append(f"{_dotfill('PR frequency (90d)', 28)} {pr_freq_90} verified peaks")
    return lines


def _build_about_stats(ttl_seconds: int = 15) -> Dict[str, Any]:
    now = time.time()
    if _ABOUT_CACHE.get("payload") is not None and (now - float(_ABOUT_CACHE.get("ts", 0.0))) < ttl_seconds:
        return dict(_ABOUT_CACHE["payload"])

    db_dir = _resolve_db_dir()
    roles = dict(DB_ROLES_DEFAULT)

    db_paths = {k: (db_dir / v) for k, v in roles.items()}

    repo_root = "/opt/liva"
    if not _is_repo_root(Path(repo_root)):
        repo_root = _resolve_repo_root()
    # Reuse the codebase line-count cache so the fake CMD can start immediately
    # instead of blocking on a full repository scan on every "about" request.
    code_lines = int(codebase_size_lines(repo_root, ttl_seconds=3600))

    selections_dates: Dict[str, Dict[str, Sequence[str]]] = {
        "training": {
            "workouts": ["date_iso"],
            "runs": ["date_iso"],
        },
        "runs": {"runs": ["date"]},
        "hrv": {"hrv_measurements": ["date_utc"]},
        "ernaehrung": {"weight_logs": ["date_iso"]},
    }

    selections_ts: Dict[str, Dict[str, Sequence[str]]] = {
        "training": {
            "workouts": ["created_at"],
            "sets": ["created_at"],
            "exercises": ["created_at"],
            "runs": ["created_at"],
        },
        "hrv": {"hrv_measurements": ["created_at"]},
        "ernaehrung": {
            "weight_logs": ["created_at"],
            "nutrition_day_actuals": ["created_at"],
        },
        "plans": {
            "plans": ["created_at", "updated_at"],
            "plan_cache": ["updated_at"],
        },
    }

    date_mins: List[datetime] = []
    date_maxs: List[datetime] = []
    for role, sel in selections_dates.items():
        p = db_paths.get(role)
        if not p:
            continue
        mn, mx = _db_bounds_selected(p, sel)
        if mn:
            date_mins.append(mn)
        if mx:
            date_maxs.append(mx)

    ts_mins: List[datetime] = []
    ts_maxs: List[datetime] = []
    for role, sel in selections_ts.items():
        p = db_paths.get(role)
        if not p:
            continue
        mn, mx = _db_bounds_selected(p, sel)
        if mn:
            ts_mins.append(mn)
        if mx:
            ts_maxs.append(mx)

    tracked_dt = min(date_mins) if date_mins else None
    tracked_since = tracked_dt.date() if tracked_dt else None

    first_record = min(ts_mins) if ts_mins else tracked_dt
    last_record = max(ts_maxs) if ts_maxs else (max(date_maxs) if date_maxs else None)

    integrity_ok = all(_quick_integrity_ok(p) for p in db_paths.values() if p.exists())
    data_integrity = "verified" if integrity_ok else "warning"

    atoms = 0
    atoms += _atoms_for_db(db_paths["training"], {"workouts": {"id"}, "exercises": {"id"}, "sets": {"id"}})
    atoms += _atoms_for_db(db_paths["runs"], {"runs": {"id"}})
    atoms += _atoms_for_db(db_paths["hrv"], {"hrv_measurements": {"id", "created_at", "source_file"}})
    atoms += _atoms_for_db(
        db_paths["ernaehrung"],
        {
            "nutrition_day_actuals": {"id"},
            "nutrition_day_plan_meals": {"id"},
            "nutrition_day_plan_slots": {"id"},
            "nutrition_foods": {"id"},
            "nutrition_meal_templates": {"id"},
            "nutrition_meal_template_items": {"id"},
            "nutrition_meal_template_estimates": {"id"},
            "nutrition_week_plans": {"id"},
            "nutrition_mode_timeline": {"id"},
            "weight_logs": {"id", "raw_json"},
            "weight_logs_deleted": {"id", "raw_json"},
        },
    )
    atoms += _atoms_for_db(db_paths["plans"], {"plans": {"id"}, "plan_cache": {"id"}, "exercise_muscle_map": set()})

    backup_status, backup_last = _backup_info()

    payload: Dict[str, Any] = {
        "owner": "LIVA operator",
        "build": "local-first / offline-capable",
        "codebase_lines": code_lines,
        "stored_values": int(atoms),
        "active_profiles": 1,
        "data_integrity": data_integrity,
        "tracked_since": _fmt_date(tracked_since),
        "first_record": _fmt_dt_min(first_record),
        "last_record": _fmt_dt_min(last_record),
        "backup_status": backup_status,
        "backup_last": _fmt_dt_min(backup_last),
        "export_mode": "available (CSV/JSON)",
        "offline_status": "ready (no external dependency)",
        "generated_at": _fmt_dt_min(_now()),
    }

    _ABOUT_CACHE["ts"] = now
    _ABOUT_CACHE["payload"] = dict(payload)
    return payload


@bp_about_stats.get("/api/about_stats")
def api_about_stats():
    return jsonify(_build_about_stats())

@bp_about_stats.post("/api/system/restart_liva")
def api_restart_liva_service():
    """
    Restart LIVA systemd service (intended for local Raspberry Pi deployments).
    Safety:
      - Only allows restarting the fixed unit liva.service.
      - Tries systemctl directly if running as root, else sudo -n (non-interactive).
    """
    data = request.get_json(silent=True) or {}
    unit = str(data.get("unit") or "").strip() or "liva.service"
    if unit != "liva.service":
        return jsonify({"ok": False, "error": "Only 'liva.service' is allowed."}), 400

    password = str(data.get("password") or "").strip()
    if password != "1909":
        return jsonify({"ok": False, "error": "Password required."}), 401

    cmd = ["systemctl", "restart", unit]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", "systemctl", "restart", unit]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if p.returncode == 0:
            return jsonify({"ok": True})
        err = (p.stderr or p.stdout or "").strip()
        return jsonify({"ok": False, "error": err or f"Command failed (exit {p.returncode})."}), 500
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    except subprocess.TimeoutExpired:
        # Even if restart is in progress, systemctl may hang if dbus/systemd is unhealthy.
        return jsonify({"ok": False, "error": "Restart timed out."}), 504

@bp_about_stats.get("/api/about_schema")
def api_about_schema():
    sample = _build_about_stats()
    return jsonify({"keys": sorted(sample.keys())})


DIAGNOSTIC_SEQUENCE: List[str] = [
    "about",
    "stats --days 90",
    "pr --recent 50",
    "progress --compare 90",
    "capacity --days 90",
    "capacity --by muscle --days 90",
    "compare avg --assumptions",
    "compare avg --days 90",
    "proof --days 90",
]


def _run_diagnostic() -> Dict[str, Any]:
    lines: List[str] = []
    tz = ZoneInfo("Europe/Berlin")
    now = datetime.now(tz)
    lines.append("DIAGNOSTIC REPORT")
    lines.append(f"Timestamp: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append("")

    success = 0
    fail = 0
    failed_cmds: List[str] = []

    for cmd in DIAGNOSTIC_SEQUENCE:
        lines.append(f">>> {cmd}")
        result = _interpret_terminal_command(cmd)
        cmd_lines = result.get("lines") or []
        if cmd_lines:
            lines.extend(cmd_lines)
        else:
            lines.append("(no output)")
        if result.get("ok"):
            success += 1
        else:
            fail += 1
            failed_cmds.append(cmd)
            lines.append(f"ERROR running {cmd}: see above")
        lines.append("----------------------------------------")
        time.sleep(0.05)

    lines.append("")
    lines.append(f"Summary: {success} succeeded, {fail} failed")
    if failed_cmds:
        lines.append(f"Failed: {', '.join(failed_cmds)}")

    return {"ok": fail == 0, "lines": lines}


def _interpret_terminal_command(cmd_raw: str) -> Dict[str, Any]:
    cmd = str(cmd_raw or "").strip()
    low = cmd.lower()

    if not low:
        return {"ok": True, "lines": []}

    parts = cmd.split()
    head = parts[0].lower() if parts else ""

    def _lev(a: str, b: str) -> int:
        a = a or ""
        b = b or ""
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, start=1):
            cur = [i]
            for j, cb in enumerate(b, start=1):
                ins = cur[j - 1] + 1
                dele = prev[j] + 1
                sub = prev[j - 1] + (0 if ca == cb else 1)
                cur.append(min(ins, dele, sub))
            prev = cur
        return prev[-1]

    def _suggest_flag(flag: str, allowed: Sequence[str]) -> Optional[str]:
        cands = [a for a in allowed if a.startswith("--")]
        if not cands:
            return None
        scored = sorted((( _lev(flag, c), c) for c in cands), key=lambda x: x[0])
        if scored and scored[0][0] <= 3:
            return scored[0][1]
        return None

    def _unknown_flag_error(flag: str, allowed: Sequence[str]) -> Dict[str, Any]:
        sug = _suggest_flag(flag, allowed)
        if sug:
            return {"ok": False, "lines": [f"Unknown flag '{flag}'. Did you mean '{sug}'?"]}
        return {"ok": False, "lines": [f"Unknown flag '{flag}'. Type 'help' for usage."]}

    def _collect_flags(args: Sequence[str]) -> List[str]:
        return [a for a in args if str(a).startswith("--")]

    if low in {"help", "?"}:
        return {"ok": True, "lines": ["Available commands:", *_build_help_lines()]}

    if low in {"debug", "diag"}:
        return _run_diagnostic()

    if head in {"about", "about.exe"}:
        return {"ok": True, "lines": _about_report_lines()}

    norm = " ".join(cmd.split()).lower()
    if norm == "restart" or norm == "systemctl restart liva.service" or norm == "sudo systemctl restart liva.service":
        # Frontend handles the actual restart animation; returning OK keeps it non-error for other clients.
        return {"ok": True, "lines": ["", "Restarting liva.service...", ""]}

    if head == "pr":
        if len(parts) >= 3 and parts[1].lower() == "--recent":
            try:
                n = int(parts[2])
            except Exception:
                n = 10
            return {"ok": True, "lines": ["", *_build_recent_peaks_lines(n)]}
        return {"ok": True, "lines": ["", *_build_pr_lines()]}

    if head == "progress":
        if len(parts) >= 3 and parts[1].lower() == "--compare":
            try:
                n = int(parts[2])
            except Exception:
                n = 90
        else:
            n = 90
        return {"ok": True, "lines": ["", *_build_progress_lines(n)]}

    if head == "capacity":
        args = parts[1:]
        allowed = {"--days", "--since", "--by"}
        for f in _collect_flags(args):
            if f.lower() not in allowed:
                return _unknown_flag_error(f, sorted(allowed))

        by_muscle = len(args) >= 2 and args[0].lower() == "--by" and args[1].lower() == "muscle"
        win, err = parse_window(args, date.today(), default_days=90)
        if err:
            return {"ok": False, "lines": [err]}
        if by_muscle:
            return {"ok": True, "lines": ["", *_build_capacity_by_muscle_lines(win)]}
        return {"ok": True, "lines": ["", *_build_capacity_lines(win)]}

    if head == "compare":
        # compare avg ...
        if len(parts) >= 2 and parts[1].lower() == "avg":
            args = parts[2:]
            allowed = {"--days", "--since", "--assumptions"}
            for f in _collect_flags(args):
                if f.lower() not in allowed:
                    return _unknown_flag_error(f, sorted(allowed))
            if "--assumptions" in [a.lower() for a in args]:
                return {"ok": True, "lines": ["", *_build_compare_avg_assumptions_lines()]}
            win, err = parse_window(args, date.today(), default_days=90)
            if err:
                return {"ok": False, "lines": [err]}
            return {"ok": True, "lines": ["", *_build_compare_avg_lines(win)]}

    if head == "proof":
        args = parts[1:]
        allowed = {"--alltime", "--days", "--since"}
        for f in _collect_flags(args):
            if f.lower() not in allowed:
                return _unknown_flag_error(f, sorted(allowed))

        if "--alltime" in [a.lower() for a in args]:
            if "--days" in [a.lower() for a in args] or "--since" in [a.lower() for a in args]:
                return {"ok": False, "lines": ["Use either --alltime or a window (--days/--since), not both."]}
            all_win = {"start_date": date(1900, 1, 1), "end_date": date.today(), "days": (date.today() - date(1900, 1, 1)).days + 1, "label": "All-Time"}
            return {"ok": True, "lines": ["", *_build_proof_lines(all_win)]}

        if not args:
            all_win = {"start_date": date(1900, 1, 1), "end_date": date.today(), "days": (date.today() - date(1900, 1, 1)).days + 1, "label": "All-Time"}
            return {"ok": True, "lines": ["", *_build_proof_lines(all_win)]}

        win, err = parse_window(args, date.today(), default_days=90)
        if err:
            return {"ok": False, "lines": [err]}
        return {"ok": True, "lines": ["", *_build_proof_lines(win)]}

    if head == "stats":
        args = parts[1:]
        allowed = {"--days", "--since"}
        for f in _collect_flags(args):
            if f.lower() not in allowed:
                return _unknown_flag_error(f, sorted(allowed))
        win, err = parse_window(args, date.today(), default_days=90)
        if err:
            return {"ok": False, "lines": [err]}
        return {"ok": True, "lines": ["", *_build_stats_lines(win)]}

    if head in {"cls", "clear"}:
        return {"ok": True, "lines": []}

    return {
        "ok": True,
        "lines": [
            f"'{cmd}' is not recognized as an internal or external command,",
            "operable program or batch file.",
            "Type 'help' for available commands.",
        ],
    }


@bp_about_stats.post("/api/terminal/exec")
def api_terminal_exec():
    data = request.get_json(silent=True) or {}
    cmd_raw = str(data.get("cmd") or "")
    return jsonify(_interpret_terminal_command(cmd_raw))
