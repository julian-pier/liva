from __future__ import annotations

import json
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit


MAX_ACTION_RESPONSE_BYTES = 262_144


class _NoRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ActionsBridge:
    """Narrow loopback client for the canonical LIVA LIVA facades."""

    def __init__(self, base_url: str | None, token: str | None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        if not self.base_url:
            return
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Actions bridge URL must be an HTTP loopback origin")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("canonical_actions_bridge_not_configured")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 131_072:
            raise ValueError("Actions payload exceeds the size limit")
        req = request.Request(
            self.base_url + path,
            data=encoded,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "X-LIVA-MCP-Bridge": "1",
            },
        )
        opener = request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=45) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read(MAX_ACTION_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            status = int(exc.code or 500)
            raw = exc.read(MAX_ACTION_RESPONSE_BYTES + 1)
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError("canonical_actions_bridge_unavailable") from exc
        if len(raw) > MAX_ACTION_RESPONSE_BYTES:
            raise RuntimeError("canonical_actions_response_too_large")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("canonical_actions_invalid_response") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("canonical_actions_invalid_response")
        if status >= 400 or parsed.get("ok") is False:
            error_payload = parsed.get("error")
            if isinstance(error_payload, dict):
                code = str(error_payload.get("code") or "canonical_action_rejected")
                message = str(error_payload.get("message") or code)
            else:
                code = str(error_payload or "canonical_action_rejected")
                message = str(parsed.get("detail") or code)
            if 400 <= status < 500:
                raise ValueError(f"{code}: {message}")
            raise RuntimeError("canonical_actions_upstream_failed")
        return parsed

    def read(self, mode: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {**payload, "mode": mode}
        if "mode" in payload:
            nested_payload = payload.get("payload")
            body["payload"] = {
                **(nested_payload if isinstance(nested_payload, dict) else {}),
                "mode": payload["mode"],
            }
        response = self._post("/api/v2/actions/liva/read", body)
        response.pop("ok", None)
        return {
            **response,
            "source": "canonical_actions",
            "production_read": True,
            "readback_source": "liva.actions_v2",
        }

    def act(
        self,
        domain: str,
        command: str,
        payload: dict[str, Any],
        *,
        dry_run: bool,
        confirm: bool,
        reason: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "domain": domain,
            "command": command,
            "payload": payload,
            "dry_run": dry_run,
            "confirm": confirm,
            # Large endurance plans can exceed the gateway response ceiling even
            # though the canonical mutation succeeded. Ask for the bounded MCP
            # receipt instead of echoing the complete plan back over the bridge.
            "response_profile": "mcp_compact",
        }
        if reason:
            body["reason"] = reason
            body["payload"] = {**payload, "reason": reason}
        response = self._post("/api/v2/actions/liva/act", body)
        response.pop("ok", None)
        execution = response.get("execution") if isinstance(response.get("execution"), dict) else {}
        live_changed = bool(execution.get("live_state_changed"))
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
        plan_id = plan.get("id") or payload.get("endurance_plan_id")
        affected_ids = response.get("affected_ids") if isinstance(response.get("affected_ids"), list) else ([plan_id] if plan_id else [])
        return {
            **response,
            "status": "success" if not dry_run else "dry_run",
            "dry_run": dry_run,
            "source": "canonical_actions",
            "production_write": not dry_run,
            "executed": live_changed,
            "changed": live_changed,
            "affected_ids": affected_ids,
            "visible_in_frontend": bool(live_changed),
            "visible_in_daily_snapshot": False,
            "readback_source": "liva.actions_v2",
        }

    def refresh_training_card(self, date_iso: str) -> dict[str, Any]:
        """Invalidate frontend snapshots after a direct canonical card write."""
        response = self._post(
            "/api/v2/actions/training/today/card/refresh",
            {"date": str(date_iso or "").strip()},
        )
        response.pop("ok", None)
        return {
            **response,
            "source": "canonical_actions",
            "readback_source": "liva.training_today_card",
        }
