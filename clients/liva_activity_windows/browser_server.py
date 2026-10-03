from __future__ import annotations

import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .browser_context import BrowserContextStore
from .models import BrowserContext

LOG = logging.getLogger(__name__)


def _timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def make_handler(store: BrowserContextStore, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "LivaActivityLoopback/1"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/browser-context":
                return self._send(404, {"ok": False})
            supplied = (self.headers.get("X-LIVA-Activity-Token") or "").strip()
            if not hmac.compare_digest(supplied, token):
                return self._send(401, {"ok": False, "error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0 or length > 16384:
                    raise ValueError("invalid content length")
                raw = json.loads(self.rfile.read(length).decode("utf-8"))
                browser = str(raw.get("browser") or "").lower()
                if browser not in {"chrome", "edge", "opera"}:
                    raise ValueError("unsupported browser")
                store.set_dashboard_open(bool(raw.get("dashboard_open")))
                raw_contexts = raw.get("contexts")
                if not isinstance(raw_contexts, list):
                    raw_contexts = [raw]
                contexts = []
                for item in raw_contexts[:20]:
                    if not isinstance(item, dict):
                        continue
                    domain = str(item.get("domain") or "").strip().lower() or None
                    if domain and (len(domain) > 253 or any(char in domain for char in "/:?#") or ".." in domain):
                        raise ValueError("invalid domain")
                    title = str(item.get("page_title") or "").strip()[:1024] or None
                    window_id = item.get("window_id")
                    contexts.append(BrowserContext(
                        browser, bool(item.get("focused")), domain, title,
                        _timestamp(item.get("timestamp") or raw.get("timestamp")),
                        int(window_id) if window_id is not None else None,
                        bool(item.get("media_playing") or item.get("audible")),
                    ))
                store.replace_browser(browser, contexts)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                return self._send(400, {"ok": False, "error": "invalid_payload", "detail": str(exc)})
            self._send(200, {"ok": True})

        def log_message(self, format: str, *args) -> None:
            LOG.debug("Browser bridge: " + format, *args)

    return Handler


def start_browser_server(store: BrowserContextStore, token: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, token))
    threading.Thread(target=server.serve_forever, name="activity-browser-bridge", daemon=True).start()
    return server
