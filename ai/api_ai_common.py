from __future__ import annotations

import base64
import json
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from flask import jsonify
from werkzeug.exceptions import HTTPException


def attach_error_handlers(bp):
    @bp.errorhandler(HTTPException)
    def _http_exception(exc: HTTPException):
        payload = {
            "ok": False,
            "error_code": "http_error",
            "message": getattr(exc, "name", "HTTPException"),
            "detail": str(getattr(exc, "description", ""))[:500],
        }
        return jsonify(payload), int(getattr(exc, "code", 500) or 500)

    @bp.errorhandler(Exception)
    def _unhandled_exception(exc: Exception):
        payload = {
            "ok": False,
            "error_code": "internal_error",
            "message": "internal_error",
            "detail": str(exc)[:500],
        }
        return jsonify(payload), 500

    return bp


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_limit(raw: Any, default: int = 200, max_limit: int = 500) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    if value <= 0:
        value = default
    return min(value, max_limit)


def _parse_last_token(token: str) -> Optional[int]:
    text = (token or "").strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    suffix = text[-1]
    number = text[:-1]
    if not number.isdigit():
        return None
    value = int(number)
    if suffix == "d":
        return value
    if suffix == "w":
        return value * 7
    if suffix == "m":
        return value * 30
    if suffix == "y":
        return value * 365
    return None


def parse_time_range(args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    date_from = (args.get("from") or "").strip()
    date_to = (args.get("to") or "").strip()
    last_token = (args.get("last") or "").strip()

    if date_from or date_to:
        return date_from or None, date_to or None, last_token or None

    days = _parse_last_token(last_token)
    if days:
        today = date.today()
        start = today - timedelta(days=max(1, days) - 1)
        return start.isoformat(), today.isoformat(), last_token

    return None, None, None


def build_date_where(column: str, date_from: Optional[str], date_to: Optional[str]) -> Tuple[List[str], List[Any]]:
    where: List[str] = []
    params: List[Any] = []
    if date_from:
        where.append(f"date({column}) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append(f"date({column}) <= date(?)")
        params.append(date_to)
    return where, params


def parse_fields(raw: Any, allowed: Sequence[str]) -> Optional[List[str]]:
    if not raw:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    else:
        return None
    fields = [p for p in parts if p in allowed]
    return fields or None


def apply_fields(rows: Iterable[Dict[str, Any]], fields: Optional[List[str]]) -> List[Dict[str, Any]]:
    if not fields:
        return [dict(r) for r in rows]
    return [{k: r.get(k) for k in fields} for r in rows]


def _b64_encode(data: Dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _b64_decode(token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + padding)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def parse_cursor(raw: Any) -> int:
    if not raw:
        return 0
    token = str(raw)
    data = _b64_decode(token) or {}
    try:
        return max(0, int(data.get("o", 0)))
    except Exception:
        return 0


def make_cursor(offset: int) -> str:
    return _b64_encode({"o": int(offset)})


def add_pagination_meta(meta: Dict[str, Any], offset: int, limit: int, returned: int) -> Dict[str, Any]:
    if returned >= limit:
        meta["cursor_next"] = make_cursor(offset + returned)
    else:
        meta["cursor_next"] = None
    meta["limit"] = limit
    return meta


def summarize_bounds(conn, table: str, date_col: str) -> Dict[str, Any]:
    try:
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT MIN(date({date_col})) AS min_date, MAX(date({date_col})) AS max_date, COUNT(*) AS n FROM {table}"
        ).fetchone()
        return {
            "min_date": row["min_date"] if row else None,
            "max_date": row["max_date"] if row else None,
            "count": row["n"] if row else 0,
        }
    except Exception:
        return {"min_date": None, "max_date": None, "count": 0}


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def utc_ts() -> int:
    return int(time.time())
