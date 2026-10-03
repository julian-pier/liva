from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT_VERSION = "LIVA-FR-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def archive_name(backup_id: str, timestamp: str | None = None) -> str:
    stamp = (timestamp or utc_now()).replace(":", "").replace("-", "")
    return f"{FORMAT_VERSION}_{stamp}_{backup_id}.tar.zst.age"


def git_reference(root: Path) -> dict[str, Any]:
    def run(args: list[str]) -> str | None:
        try:
            result = subprocess.run(args, cwd=root, text=True, capture_output=True, timeout=5, check=False)
            return result.stdout.strip() if result.returncode == 0 else None
        except OSError:
            return None
    remote = run(["git", "remote", "get-url", "origin"])
    source_commit = run(["git", "rev-parse", "refs/remotes/origin/main"])
    return {"remote": remote, "source_ref": "refs/heads/main", "source_commit": source_commit, "reproducible": bool(remote and source_commit), "warning": None if remote and source_commit else "CODE_SNAPSHOT_STALE"}


def base_manifest(*, backup_id: str, recipient_fingerprint: str, source_commit: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    return {"format_version": FORMAT_VERSION, "backup_id": backup_id, "created_at": utc_now(), "hostname": platform.node(), "os": platform.platform(), "source": source_commit, "recipient_fingerprint": recipient_fingerprint, "tool_versions": {"python": platform.python_version()}, "entries": [], "excludes": [], "inventory_drift": [], "container_images": [], "warnings": warnings, "total_size": 0, "file_count": 0}


def encode(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")

