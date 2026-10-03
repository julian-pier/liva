from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src" / "liva_mcp"


def test_forbidden_product_modules_are_not_imported():
    forbidden = {"app", "ai.actions_v2", "database.connections"}
    imported = set()
    for path in ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert imported.isdisjoint(forbidden)


def test_no_schema_or_mutation_function_names():
    forbidden = ("ensure_schema", "ensure_core", "cleanup_expired", "sync_", "build_core", "save_", "promote_", "control_")
    function_names = set()
    for path in ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function_names.update(
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    assert not any(marker in name for marker in forbidden for name in function_names)
