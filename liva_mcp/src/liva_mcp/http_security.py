from __future__ import annotations

import asyncio
import hashlib
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import parse_qs


MAX_REQUEST_BYTES = 65_536
# MCP tool results can legitimately contain several structured readbacks.  Keep
# a finite transport guard, but do not turn valid coach responses into 500s.
MAX_RESPONSE_BYTES = 1_048_576
ALLOWED_METHODS = {
    "/.well-known/oauth-protected-resource": {"GET", "HEAD"},
    "/.well-known/oauth-protected-resource/mcp": {"GET", "HEAD"},
    "/.well-known/oauth-authorization-server": {"GET", "HEAD"},
    "/oauth/authorize": {"GET", "POST"}, "/oauth/token": {"POST"}, "/oauth/revoke": {"POST"}, "/oauth/register": {"POST"},
    "/oauth/login": {"GET", "POST"}, "/mcp": {"GET", "POST"}, "/healthz": {"GET"},
}


class WindowLimiter:
    def __init__(self, limit: int, seconds: int):
        self.limit = limit; self.seconds = seconds
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
        async with self.lock:
            if key not in self.events and len(self.events) >= 4096:
                self.events.pop(next(iter(self.events)))
            bucket = self.events[key]
            while bucket and bucket[0] <= now - self.seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True


class SecurityMiddleware:
    def __init__(self, app: Any, public_host: str, resource: str, metadata_url: str, concurrency: int = 8):
        self.app = app; self.public_host = public_host; self.resource = resource; self.metadata_url = metadata_url
        self.semaphore = asyncio.Semaphore(concurrency)
        self.global_limiter = WindowLimiter(180, 60); self.token_limiter = WindowLimiter(60, 60)
        self.register_limiter = WindowLimiter(10, 600)

    @staticmethod
    def _headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
        handoff = any(key.lower() == b"x-liva-oauth-handoff" and value == b"1" for key, value in headers)
        login_page = any(key.lower() == b"x-liva-oauth-login" and value == b"1" for key, value in headers)
        blocked = {b"server", b"access-control-allow-origin", b"access-control-allow-credentials", b"access-control-allow-methods", b"access-control-allow-headers", b"cache-control", b"pragma", b"x-liva-oauth-handoff", b"x-liva-oauth-login"}
        clean = [(key, value) for key, value in headers if key.lower() not in blocked]
        if handoff:
            csp = b"default-src 'none'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        elif login_page:
            csp = b"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        else:
            csp = b"default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        clean.extend([
            (b"cache-control", b"no-store"), (b"pragma", b"no-cache"), (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"), (b"referrer-policy", b"no-referrer"),
            (b"content-security-policy", csp),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        ])
        return clean

    async def _plain(self, send: Any, status: int, message: str, oauth_error: str | None = None) -> None:
        body = message.encode()
        headers = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]
        if oauth_error:
            headers.append((b"x-liva-oauth-error", oauth_error.encode()))
        await send({"type": "http.response.start", "status": status, "headers": self._headers(headers)})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send); return
        path, method = scope.get("path", ""), scope.get("method", "")
        allowed = ALLOWED_METHODS.get(path)
        if allowed is None:
            await self._plain(send, 404, "Not found"); return
        if method not in allowed:
            await self._plain(send, 405, "Method not allowed"); return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("ascii", "ignore").lower()
        public_hosts = {self.public_host}
        if ":" not in self.public_host:
            # Keep the former Funnel endpoint usable while all discovery metadata
            # consistently points clients to the canonical standard-HTTPS origin.
            public_hosts.add(f"{self.public_host}:10000")
        if host not in {*public_hosts, "127.0.0.1:8765", "localhost:8765"}:
            await self._plain(send, 400, "Invalid host"); return
        peer = (scope.get("client") or ("", 0))[0]
        if any(name in headers for name in (b"x-forwarded-host", b"x-forwarded-proto", b"x-forwarded-for")) and peer not in {"127.0.0.1", "::1"}:
            await self._plain(send, 400, "Invalid proxy headers"); return
        length = headers.get(b"content-length")
        if length:
            try:
                if int(length) > MAX_REQUEST_BYTES:
                    await self._plain(send, 413, "Request too large"); return
            except ValueError:
                await self._plain(send, 400, "Invalid request"); return
        if not await self.global_limiter.allow(f"peer:{peer}"):
            await self._plain(send, 429, "Rate limit exceeded"); return
        if path == "/oauth/register" and not await self.register_limiter.allow(f"register:{peer}"):
            await self._plain(send, 429, "Rate limit exceeded"); return
        auth = headers.get(b"authorization", b"")
        if auth and not await self.token_limiter.allow(hashlib.sha256(auth).hexdigest()):
            await self._plain(send, 429, "Rate limit exceeded"); return
        size = 0
        if path == "/oauth/token":
            chunks = []
            while True:
                event = await receive()
                if event["type"] != "http.request":
                    continue
                chunks.append(event.get("body", b"")); size += len(chunks[-1])
                if size > MAX_REQUEST_BYTES:
                    await self._plain(send, 413, "Request too large"); return
                if not event.get("more_body", False):
                    break
            body = b"".join(chunks)
            try:
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            except UnicodeError:
                await self._plain(send, 400, "Invalid request"); return
            resource = fields.get("resource", [""])[0]
            if resource and resource != self.resource:
                await self._plain(send, 400, "Invalid OAuth resource", "resource_mismatch"); return
            delivered_request = False
            async def replay_receive() -> dict[str, Any]:
                nonlocal delivered_request
                if not delivered_request:
                    delivered_request = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}
            receive = replay_receive
            size = 0
        async def limited_receive() -> dict[str, Any]:
            nonlocal size
            event = await receive()
            if event["type"] == "http.request":
                size += len(event.get("body", b""))
                if size > MAX_REQUEST_BYTES:
                    raise ValueError
            return event
        started: dict[str, Any] | None = None; response_parts: list[bytes] = []; delivered = False
        async def buffered_send(event: dict[str, Any]) -> None:
            nonlocal started, delivered
            if event["type"] == "http.response.start":
                started = event
            elif event["type"] == "http.response.body":
                response_parts.append(event.get("body", b""))
                if sum(map(len, response_parts)) > MAX_RESPONSE_BYTES:
                    raise OverflowError
                if not event.get("more_body", False):
                    assert started is not None
                    outgoing = dict(started); response_headers = list(started.get("headers", []))
                    if path == "/mcp" and outgoing["status"] in {401, 403}:
                        response_headers = [(key, value) for key, value in response_headers if key.lower() != b"www-authenticate"]
                        challenge = f'Bearer resource_metadata="{self.metadata_url}", scope="liva.read liva.write"'
                        if outgoing["status"] == 403:
                            challenge += ', error="insufficient_scope"'
                        response_headers.append((b"www-authenticate", challenge.encode()))
                    outgoing["headers"] = self._headers(response_headers)
                    await send(outgoing); await send({"type": "http.response.body", "body": b"".join(response_parts)}); delivered = True
        try:
            async with self.semaphore:
                async with asyncio.timeout(25):
                    await self.app(scope, limited_receive, buffered_send)
        except ValueError:
            if not delivered: await self._plain(send, 413, "Request too large")
        except OverflowError:
            if not delivered: await self._plain(send, 500, "Response exceeds limit")
        except Exception:
            if not delivered: await self._plain(send, 500, "Internal server error")
