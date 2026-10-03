from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from memory.errors import MemoryError
from memory.memory_markdown import render_markdown
from memory.memory_models import FOLDER_ORDER, LOGICAL_FILE_SPECS, ROOT_DIRNAME_DEFAULT, build_document_template, get_repo_root


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class MemoryStorage:
    def __init__(self, root_dir: str | Path | None = None):
        default_root = get_repo_root() / "data" / ROOT_DIRNAME_DEFAULT
        self.root_dir = Path(root_dir or (os.getenv("LIVA_MEMORY_DIR") or default_root))

    def get_config_status(self) -> dict[str, Any]:
        parent = self.root_dir if self.root_dir.exists() else self.root_dir.parent
        writable = os.access(parent, os.W_OK) if parent.exists() else os.access(get_repo_root(), os.W_OK)
        warnings: list[str] = []
        if not writable:
            warnings.append(f"Memory root is not writable: {parent}")
        return {
            "memory_enabled": _env_flag("MEMORY_ENABLED", True),
            "configured": True,
            "storage_mode": "local_markdown",
            "root_path": str(self.root_dir),
            "root_exists": self.root_dir.exists(),
            "writable": writable,
            "warnings": warnings,
            "missing_runtime": [],
        }

    def initialize_storage(self, *, create_missing: bool) -> dict[str, Any]:
        config = self.get_config_status()
        if not config["memory_enabled"]:
            raise MemoryError("memory_disabled", "Set MEMORY_ENABLED=1 to enable the memory module.", 503)
        if create_missing:
            self.root_dir.mkdir(parents=True, exist_ok=True)
        folders: list[dict[str, Any]] = []
        for folder_name in FOLDER_ORDER:
            folder_path = self.root_dir / folder_name
            if create_missing:
                folder_path.mkdir(parents=True, exist_ok=True)
            folders.append({"name": folder_name, "path": str(folder_path), "found": folder_path.exists()})
        logical_files = []
        for logical_name, spec in LOGICAL_FILE_SPECS.items():
            path = self.logical_file_path(logical_name)
            if create_missing and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(build_document_template(spec), encoding="utf-8")
            logical_files.append(self.describe_logical_file(logical_name))
        return {
            "root_path": str(self.root_dir),
            "root_exists": self.root_dir.exists(),
            "folders": folders,
            "logical_files": logical_files,
        }

    def logical_file_path(self, logical_name: str) -> Path:
        spec = LOGICAL_FILE_SPECS.get(logical_name)
        if not spec:
            raise MemoryError("memory_file_unknown", f"Unknown logical file: {logical_name}", 404)
        path = (self.root_dir / spec.folder_name / spec.filename).resolve()
        try:
            path.relative_to(self.root_dir.resolve())
        except Exception as exc:
            raise MemoryError("memory_path_invalid", f"Resolved path escaped the memory root: {logical_name}", 500) from exc
        return path

    def describe_logical_file(self, logical_name: str) -> dict[str, Any]:
        spec = LOGICAL_FILE_SPECS[logical_name]
        path = self.logical_file_path(logical_name)
        exists = path.exists()
        stat = path.stat() if exists else None
        relative_path = Path(spec.folder_name) / spec.filename
        if exists:
            try:
                relative_path = path.relative_to(self.root_dir)
            except Exception:
                pass
        return {
            "logical_name": logical_name,
            "title": spec.title,
            "folder_name": spec.folder_name,
            "filename": spec.filename,
            "path": str(path),
            "relative_path": str(relative_path),
            "found": exists,
            "last_updated": _mtime_iso(path) if exists else None,
            "size_bytes": int(stat.st_size) if stat else 0,
        }

    def read_file(self, logical_name: str, *, create_missing: bool = False) -> dict[str, Any]:
        path = self.logical_file_path(logical_name)
        if not path.exists():
            if not create_missing:
                raise MemoryError("memory_file_missing", f"Memory file does not exist: {logical_name}", 404)
            self.initialize_storage(create_missing=True)
        raw_markdown = path.read_text(encoding="utf-8")
        return {
            **self.describe_logical_file(logical_name),
            "raw_markdown": raw_markdown,
            "rendered_html": render_markdown(raw_markdown),
        }

    def write_file(self, logical_name: str, content: str) -> dict[str, Any]:
        path = self.logical_file_path(logical_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_text(str(content), encoding="utf-8")
            tmp_path.replace(path)
        except Exception as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise MemoryError("memory_write_failed", f"Could not write memory file '{logical_name}': {exc}", 500) from exc
        return self.read_file(logical_name, create_missing=False)

    def archive_files(self, logical_files: list[str], *, reason: str = "") -> list[dict[str, Any]]:
        archive_dir = self.root_dir / "90_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        snapshots: list[dict[str, Any]] = []
        timestamp = _timestamp_slug()
        for logical_name in logical_files:
            if logical_name not in LOGICAL_FILE_SPECS:
                continue
            src = self.logical_file_path(logical_name)
            if not src.exists():
                continue
            dst = archive_dir / f"{timestamp}_{logical_name}.md"
            try:
                shutil.copy2(src, dst)
            except Exception as exc:
                raise MemoryError("memory_archive_failed", f"Could not archive '{logical_name}': {exc}", 500) from exc
            snapshots.append(
                {
                    "logical_file": logical_name,
                    "archive_name": dst.name,
                    "archive_path": str(dst),
                    "archived_at": _mtime_iso(dst),
                    "reason": reason or None,
                }
            )
        return snapshots


def _mtime_iso(path: Path) -> str | None:
    try:
        return _iso_from_epoch(path.stat().st_mtime)
    except Exception:
        return None


def _iso_from_epoch(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp_slug() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
