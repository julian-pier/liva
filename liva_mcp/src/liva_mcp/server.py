from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from . import __version__
from .config import Config
from .instructions import server_instructions
from .read_service import ReadService
from .redaction import safe_error
from .tool_registry import call_tool, list_tools


PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "liva-mcp-controlled", "version": __version__}
MAX_TOOL_RESPONSE_BYTES = 262_144
# Transaction-level helpers remain available to trusted in-process tests, but
# must never be discoverable from either public MCP transport.
PRIVATE_MEMORY_TOOLS = {"liva_memory_pending", "liva_memory_ingest_file", "liva_memory_commit"}


class McpServer:
    def __init__(self, service: ReadService):
        self.service = service

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if method == "notifications/initialized":
            return None
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": server_instructions(),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [tool for tool in list_tools() if tool["name"] not in PRIVATE_MEMORY_TOOLS]}
            elif method == "tools/call":
                params = message.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                payload = call_tool(self.service, str(params.get("name") or ""), params.get("arguments") or {})
                text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if len(text.encode("utf-8")) > MAX_TOOL_RESPONSE_BYTES:
                    payload = {"ok": False, "error": {"code": "response_too_large", "message": "Tool response exceeds the size limit"}}
                    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                result = {"content": [{"type": "text", "text": text}], "isError": not payload.get("ok", False)}
            else:
                if request_id is None:
                    return None
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": safe_error(exc)["message"]}}


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    service = ReadService(Config.from_env())
    service.overlay.initialize()
    server = McpServer(service)
    for line in stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Message must be an object")
            response = server.handle(message)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": safe_error(exc, "parse_error")["message"]}}
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
