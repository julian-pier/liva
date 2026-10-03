from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from database.connections import get_polar_db

POLAR_TOKEN_URL = "https://auth.polar.com/oauth/token"
POLAR_API_BASE_URL = "https://www.polaraccesslink.com/v4/data"


class PolarClientError(RuntimeError):
    pass


class PolarNotConfiguredError(PolarClientError):
    pass


class PolarNotConnectedError(PolarClientError):
    pass


@dataclass
class PolarTokenRecord:
    id: int
    polar_user_id: str | None
    access_token: str
    refresh_token: str | None
    token_type: str | None
    expires_at: str | None
    scope: str | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> "PolarTokenRecord":
        return cls(
            id=int(row["id"]),
            polar_user_id=row["polar_user_id"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            token_type=row["token_type"],
            expires_at=row["expires_at"],
            scope=row["scope"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def ensure_polar_schema() -> None:
    with closing(get_polar_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS polar_tokens (
                id INTEGER PRIMARY KEY,
                polar_user_id TEXT,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_type TEXT,
                expires_at TEXT,
                scope TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_sync_log (
                id INTEGER PRIMARY KEY,
                sync_type TEXT,
                status TEXT,
                message TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_sleep (
                date TEXT PRIMARY KEY,
                raw_json TEXT NOT NULL,
                sleep_score REAL,
                sleep_start TEXT,
                sleep_end TEXT,
                sleep_minutes INTEGER,
                actual_sleep_minutes INTEGER,
                deep_sleep_minutes INTEGER,
                rem_sleep_minutes INTEGER,
                interruptions INTEGER,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_nightly_recharge (
                date TEXT PRIMARY KEY,
                raw_json TEXT NOT NULL,
                ans_status REAL,
                recovery_indicator REAL,
                recovery_indicator_sublevel REAL,
                ans_rate REAL,
                mean_recovery_rri REAL,
                mean_recovery_rmssd REAL,
                mean_recovery_respiration_interval REAL,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_continuous_samples (
                date TEXT PRIMARY KEY,
                raw_json TEXT NOT NULL,
                sample_count INTEGER,
                min_hr REAL,
                max_hr REAL,
                avg_hr REAL,
                resting_candidate_hr REAL,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_activity (
                date TEXT PRIMARY KEY,
                raw_json TEXT NOT NULL,
                steps INTEGER,
                active_minutes INTEGER,
                inactivity_count INTEGER,
                calories INTEGER,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_training_sessions (
                session_id TEXT PRIMARY KEY,
                raw_json TEXT NOT NULL,
                start_time TEXT,
                stop_time TEXT,
                duration_seconds INTEGER,
                sport TEXT,
                name TEXT,
                avg_hr REAL,
                max_hr REAL,
                calories INTEGER,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS polar_smart_sync_state (
                target_date TEXT PRIMARY KEY,
                last_attempt_at TEXT,
                last_status TEXT,
                last_message TEXT
            );
            """
        )
        existing_sleep_cols = {row["name"] for row in conn.execute("PRAGMA table_info(polar_sleep)").fetchall()}
        for col_name, col_type in (
            ("light_sleep_minutes", "INTEGER"),
            ("awake_minutes", "INTEGER"),
            ("interruption_minutes", "INTEGER"),
        ):
            if col_name not in existing_sleep_cols:
                conn.execute(f"ALTER TABLE polar_sleep ADD COLUMN {col_name} {col_type}")
        conn.commit()


def log_polar_sync(sync_type: str, status: str, message: str) -> None:
    now_iso = _utc_now_iso()
    with closing(get_polar_db()) as conn:
        conn.execute(
            """
            INSERT INTO polar_sync_log (sync_type, status, message, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (sync_type, status, _sanitize_log_message(message)[:1000], now_iso),
        )
        conn.commit()


class PolarClient:
    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str | None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.redirect_uri = (redirect_uri or "").strip()
        self.timeout_seconds = float(timeout_seconds)

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def require_configured(self) -> None:
        missing: list[str] = []
        if not self.client_id:
            missing.append("POLAR_CLIENT_ID")
        if not self.client_secret:
            missing.append("POLAR_CLIENT_SECRET")
        if not self.redirect_uri:
            missing.append("POLAR_REDIRECT_URI")
        if missing:
            raise PolarNotConfiguredError(f"Polar-Konfiguration fehlt: {', '.join(missing)}")

    def get_saved_token(self) -> PolarTokenRecord | None:
        ensure_polar_schema()
        with closing(get_polar_db()) as conn:
            row = conn.execute(
                """
                SELECT id, polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at
                FROM polar_tokens
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return PolarTokenRecord.from_row(row) if row else None

    def needs_reconnect(self) -> bool:
        token = self.get_saved_token()
        if not token:
            return True
        if not token.refresh_token:
            return True
        return self._last_error_requires_reconnect(since=token.updated_at)

    def disconnect(self) -> int:
        ensure_polar_schema()
        with closing(get_polar_db()) as conn:
            cur = conn.execute("DELETE FROM polar_tokens")
            conn.commit()
            deleted = int(cur.rowcount or 0)
        log_polar_sync("disconnect", "ok", "Lokale Polar-Tokens entfernt.")
        return deleted

    def is_expired(self, token: PolarTokenRecord, *, leeway_seconds: int = 60) -> bool:
        if not token.expires_at:
            return False
        try:
            expires_at = datetime.fromisoformat(token.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return expires_at <= datetime.now(timezone.utc) + timedelta(seconds=max(0, leeway_seconds))

    def exchange_code(self, code: str) -> PolarTokenRecord:
        self.require_configured()
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        data = self._request_token(payload)
        return self._store_token_payload(data)

    def refresh_access_token(self, refresh_token: str | None) -> PolarTokenRecord:
        self.require_configured()
        if not refresh_token:
            raise PolarClientError("Kein Refresh-Token verfügbar.")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        data = self._request_token(payload)
        return self._store_token_payload(data)

    def get_valid_token(self) -> PolarTokenRecord:
        token = self.get_saved_token()
        if not token:
            raise PolarNotConnectedError("Polar ist noch nicht verbunden.")
        if not self.is_expired(token):
            return token
        if not token.refresh_token:
            self._mark_reconnect_needed()
            raise PolarClientError("Polar nicht mehr autorisiert: bitte neu verbinden.")
        try:
            return self.refresh_access_token(token.refresh_token)
        except (PolarClientError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            # A rejected refresh token cannot recover without a new OAuth grant.
            # Normalise the provider error so callers can show the reconnect state
            # instead of crashing or treating the import as a successful request.
            self._mark_reconnect_needed()
            raise PolarClientError("Polar nicht mehr autorisiert: bitte neu verbinden.") from exc

    def authorization_header(self) -> dict[str, str]:
        token = self.get_valid_token()
        token_type = (token.token_type or "Bearer").strip().title()
        return {"Authorization": f"{token_type} {token.access_token}"}

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._authorized_json_request(path, params=params, retry_on_401=True)

    def get_sleeps(self, from_date: str, to_date: str, features: list[str] | None = None) -> Any:
        return self._get_range_json("/sleeps", from_date, to_date, features=features)

    def get_nightly_recharge(self, from_date: str, to_date: str, features: list[str] | None = None) -> Any:
        return self._get_range_json("/nightly-recharge-results", from_date, to_date, features=features)

    def get_continuous_samples(self, from_date: str, to_date: str, features: list[str] | None = None) -> Any:
        return self._get_range_json("/continuous-samples", from_date, to_date, features=features)

    def get_activity(self, from_date: str, to_date: str, features: list[str] | None = None) -> Any:
        return self._get_range_json("/activity/list", from_date, to_date, features=features)

    def get_training_sessions(self, from_date: str, to_date: str, features: list[str] | None = None) -> Any:
        return self._get_range_json(
            "/training-sessions/list",
            _as_iso_datetime_boundary(from_date, with_timezone=False),
            _as_iso_datetime_boundary(to_date, with_timezone=False),
            features=features,
        )

    def _get_range_json(self, path: str, from_date: str, to_date: str, *, features: list[str] | None = None) -> Any:
        params: dict[str, Any] = {"from": from_date, "to": to_date}
        if features:
            cleaned_features = [str(item).strip() for item in features if str(item).strip()]
            if cleaned_features:
                params["features"] = cleaned_features
        return self.get_json(path, params=params)

    def _store_token_payload(self, payload: dict[str, Any]) -> PolarTokenRecord:
        ensure_polar_schema()
        now_iso = _utc_now_iso()
        expires_at = _expires_at_from_payload(payload)
        scope = str(payload.get("scope") or "").strip() or None
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise PolarClientError("Polar hat kein Access-Token zurückgegeben.")
        refresh_token = str(payload.get("refresh_token") or "").strip() or None
        token_type = str(payload.get("token_type") or "bearer").strip() or "bearer"
        polar_user_id = self._resolve_polar_user_id(access_token, token_type, scope)
        with closing(get_polar_db()) as conn:
            existing = conn.execute("SELECT id, created_at FROM polar_tokens ORDER BY id DESC LIMIT 1").fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE polar_tokens
                    SET polar_user_id = ?, access_token = ?, refresh_token = ?, token_type = ?, expires_at = ?, scope = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        polar_user_id,
                        access_token,
                        refresh_token,
                        token_type,
                        expires_at,
                        scope,
                        now_iso,
                        int(existing["id"]),
                    ),
                )
                token_id = int(existing["id"])
                created_at = existing["created_at"] or now_iso
            else:
                cur = conn.execute(
                    """
                    INSERT INTO polar_tokens (
                        polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (polar_user_id, access_token, refresh_token, token_type, expires_at, scope, now_iso, now_iso),
                )
                token_id = int(cur.lastrowid)
                created_at = now_iso
            conn.commit()
            row = conn.execute(
                """
                SELECT id, polar_user_id, access_token, refresh_token, token_type, expires_at, scope, created_at, updated_at
                FROM polar_tokens
                WHERE id = ?
                """,
                (token_id,),
            ).fetchone()
        return PolarTokenRecord.from_row(row) if row else PolarTokenRecord(
            id=token_id,
            polar_user_id=polar_user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type=token_type,
            expires_at=expires_at,
            scope=scope,
            created_at=created_at,
            updated_at=now_iso,
        )

    def _resolve_polar_user_id(self, access_token: str, token_type: str, scope: str | None) -> str | None:
        scope_values = set((scope or "").split())
        if "profile:read" not in scope_values:
            return None
        req = urllib.request.Request(
            f"{POLAR_API_BASE_URL}/user/account-data",
            headers={
                "Accept": "application/json",
                "User-Agent": "LIVA/PolarPhase1",
                "Authorization": f"{(token_type or 'Bearer').strip().title()} {access_token}",
            },
            method="GET",
        )
        try:
            data = self._load_json_response(req)
        except (PolarClientError, urllib.error.HTTPError, urllib.error.URLError):
            return None
        account = data.get("accountData") if isinstance(data, dict) else None
        basic = account.get("basicInfo") if isinstance(account, dict) else None
        if not isinstance(basic, dict):
            return None
        email = str(basic.get("email") or "").strip()
        nickname = str(basic.get("nickname") or "").strip()
        if email:
            return email
        return nickname or None

    def _request_token(self, payload: dict[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            POLAR_TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {_basic_auth(self.client_id, self.client_secret)}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "LIVA/PolarPhase1",
            },
            method="POST",
        )
        data = self._load_json_response(req)
        if not isinstance(data, dict):
            raise PolarClientError("Polar Token-Antwort ist ungültig.")
        return data

    def _authorized_json_request(self, path: str, params: dict[str, Any] | None = None, *, retry_on_401: bool) -> Any:
        req = self._build_json_request(path, params=params)
        try:
            return self._load_json_response(req)
        except urllib.error.HTTPError as exc:
            if int(getattr(exc, "code", 0) or 0) != 401 or not retry_on_401:
                raise self._polar_http_error(exc) from exc
            token = self.get_saved_token()
            refresh_token = token.refresh_token if token else None
            if not refresh_token:
                self._mark_reconnect_needed()
                raise PolarClientError("Polar nicht mehr autorisiert: bitte neu verbinden.") from exc
            try:
                self.refresh_access_token(refresh_token)
            except PolarClientError as refresh_exc:
                self._mark_reconnect_needed()
                raise PolarClientError("Polar nicht mehr autorisiert: bitte neu verbinden.") from refresh_exc
            log_polar_sync("token_refresh", "ok", "Polar Token automatisch erneuert.")
            retry_req = self._build_json_request(path, params=params)
            try:
                return self._load_json_response(retry_req)
            except urllib.error.HTTPError as retry_exc:
                raise self._polar_http_error(retry_exc) from retry_exc

    def _build_json_request(self, path: str, params: dict[str, Any] | None = None) -> urllib.request.Request:
        query = urllib.parse.urlencode(
            {k: v for k, v in (params or {}).items() if v not in (None, "")},
            doseq=True,
        )
        url = f"{POLAR_API_BASE_URL}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        return urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "LIVA/PolarPhase1",
                **self.authorization_header(),
            },
            method="GET",
        )

    def _load_json_response(self, req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                if int(getattr(resp, "status", 200) or 200) == 204:
                    return []
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if int(getattr(exc, "code", 0) or 0) == 204:
                return []
            raise
        except urllib.error.URLError as exc:
            raise PolarClientError(f"Polar ist nicht erreichbar: {exc.reason}") from exc
        except Exception as exc:
            raise PolarClientError(f"Polar-Anfrage fehlgeschlagen: {exc}") from exc
        if not raw.strip():
            return []
        try:
            return json.loads(raw) if raw else {}
        except Exception as exc:
            raise PolarClientError("Polar-Antwort ist kein gültiges JSON.") from exc

    def _polar_http_error(self, exc: urllib.error.HTTPError) -> PolarClientError:
        if int(getattr(exc, "code", 0) or 0) == 401:
            detail = _safe_http_error_detail(exc)
            if detail:
                return PolarClientError(f"Polar HTTP 401: {detail}")
            return PolarClientError("Polar nicht mehr autorisiert: bitte neu verbinden.")
        detail = _safe_http_error_detail(exc)
        return PolarClientError(f"Polar HTTP {exc.code}: {detail}")

    def _last_error_requires_reconnect(self, *, since: str | None = None) -> bool:
        ensure_polar_schema()
        clauses = ["lower(status) = 'error'"]
        params: list[str] = []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        with closing(get_polar_db()) as conn:
            row = conn.execute(
                f"""
                SELECT status, message
                FROM polar_sync_log
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if not row:
            return False
        message = str(row["message"] or "").lower()
        return "reconnect" in message or "nicht mehr autorisiert" in message

    def _mark_reconnect_needed(self) -> None:
        log_polar_sync("token_refresh", "error", "Polar Reconnect nötig.")


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _safe_http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = str(exc)
    text = _strip_html(raw)
    if not text:
        text = str(exc.reason or "").strip()
    return _sanitize_log_message(text[:300] or "unbekannter Fehler")


def _expires_at_from_payload(payload: dict[str, Any]) -> str | None:
    expires_in = payload.get("expires_in")
    try:
        seconds = int(expires_in)
    except Exception:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))).isoformat()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_log_message(message: str) -> str:
    cleaned = str(message or "")
    for marker in ("access_token", "refresh_token", "Authorization", "Bearer "):
        cleaned = cleaned.replace(marker, "[redacted]")
    return cleaned


def _strip_html(message: str) -> str:
    text = str(message or "")
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _as_iso_datetime_boundary(value: str, *, with_timezone: bool = True) -> str:
    text = str(value or "").strip()
    if "T" in text:
        normalized = text
    else:
        normalized = f"{text}T00:00:00"
    if with_timezone:
        if normalized.endswith("Z") or "+" in normalized[10:]:
            return normalized
        return f"{normalized}Z"
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
    if "+" in normalized[10:]:
        normalized = normalized.split("+", 1)[0]
    return normalized
