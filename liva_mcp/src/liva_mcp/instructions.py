from __future__ import annotations

from importlib.resources import files
from .memory_skill import MEMORY_SKILL


def server_instructions() -> str:
    """Return the single packaged agent policy consumed by every MCP transport."""
    return files("liva_mcp").joinpath("agent_policy.md").read_text(encoding="utf-8") + "\n\n## Memory skill\n\n" + MEMORY_SKILL
