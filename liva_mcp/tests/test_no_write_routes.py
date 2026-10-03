from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src" / "liva_mcp"


def test_http_requests_are_literal_get_only():
    tree = ast.parse((ROOT / "read_service.py").read_text(encoding="utf-8"))
    methods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Request":
            for keyword in node.keywords:
                if keyword.arg == "method":
                    assert isinstance(keyword.value, ast.Constant)
                    methods.append(keyword.value.value)
    assert methods == ["GET"]


def test_no_generic_dispatch_names_exist():
    source = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.glob("*.py"))
    forbidden = ("call_action", "execute_command", "dynamic_dispatch", "requests.post", "requests.put", "requests.patch", "requests.delete")
    assert not any(item in source for item in forbidden)
