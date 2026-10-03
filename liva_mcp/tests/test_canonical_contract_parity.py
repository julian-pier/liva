from __future__ import annotations

import ast
from pathlib import Path

from liva_mcp.tool_registry import COACH_ACT_COMMANDS, COACH_READ_MODES


ROOT = Path(__file__).resolve().parents[2]


def _literal_assignment(name: str):
    tree = ast.parse((ROOT / "ai" / "actions_v2.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


def test_known_mcp_contract_matches_canonical_coach_facade():
    canonical_reads = _literal_assignment("LIVA_READ_MODES") - {"memory"}
    canonical_commands = _literal_assignment("LIVA_ACT_COMMANDS")
    assert COACH_READ_MODES == canonical_reads
    assert COACH_ACT_COMMANDS == {
        domain: canonical_commands[domain]
        for domain in ("core", "training", "nutrition", "weight", "cardio", "recovery", "endurance")
    }
