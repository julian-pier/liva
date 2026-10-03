#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import socket
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path("/opt/liva")
BACKUP_DIR = Path("/var/lib/liva/backups/liva-db")
DB_GLOBS = (
    ("database", "*.sqlite3"),
    (".", "*.sqlite3"),
)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    size: int
    mtime: float


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def discover_databases(project_root: Path = PROJECT_ROOT) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for rel_dir, pattern in DB_GLOBS:
        base = project_root / rel_dir
        if not base.exists():
            continue
        for path in sorted(base.glob(pattern)):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
    return found


def collect_source_files(paths: Sequence[Path]) -> list[SourceFile]:
    out: list[SourceFile] = []
    for path in paths:
        stat = path.stat()
        out.append(SourceFile(path=path, size=int(stat.st_size), mtime=float(stat.st_mtime)))
    return out


def manifest_payload(source_files: Sequence[SourceFile], *, created_at: datetime, project_root: Path = PROJECT_ROOT) -> dict[str, object]:
    entries = []
    for item in source_files:
        try:
            rel_path = item.path.relative_to(project_root)
            path_text = str(rel_path)
        except ValueError:
            path_text = str(item.path)
        entries.append(
            {
                "path": path_text,
                "size": item.size,
                "mtime": datetime.fromtimestamp(item.mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
            }
        )
    return {
        "created_at": created_at.replace(microsecond=0).isoformat(),
        "hostname": socket.gethostname(),
        "source_files": entries,
    }


def build_archive_name(created_at: datetime) -> str:
    return f"liva-db-backup_{created_at.strftime('%Y-%m-%d_%H%M%S')}.tar.gz"


def ensure_backup_dir(backup_dir: Path = BACKUP_DIR) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)


def create_backup_archive(
    *,
    project_root: Path = PROJECT_ROOT,
    backup_dir: Path = BACKUP_DIR,
    now: datetime | None = None,
) -> Path:
    db_paths = discover_databases(project_root)
    if not db_paths:
        raise RuntimeError("Keine SQLite-Datenbanken gefunden")
    created_at = now or utc_now()
    ensure_backup_dir(backup_dir)
    archive_name = build_archive_name(created_at)
    final_path = backup_dir / archive_name
    tmp_path = backup_dir / f"{archive_name}.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    source_files = collect_source_files(db_paths)
    manifest = manifest_payload(source_files, created_at=created_at, project_root=project_root)

    try:
        with tarfile.open(tmp_path, "w:gz") as tar:
            for item in source_files:
                arcname = item.path.relative_to(project_root)
                tar.add(item.path, arcname=str(arcname), recursive=False)
            manifest_bytes = (json.dumps(manifest, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            info.mtime = created_at.timestamp()
            tar.addfile(info, io.BytesIO(manifest_bytes))
        tmp_path.replace(final_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    cleanup_old_backups(backup_dir, now=created_at)
    return final_path


def cleanup_old_backups(backup_dir: Path = BACKUP_DIR, *, now: datetime | None = None) -> int:
    current = now or utc_now()
    cutoff = current - timedelta(days=14)
    removed = 0
    for path in backup_dir.glob("liva-db-backup_*.tar.gz"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def main() -> int:
    try:
        archive_path = create_backup_archive()
    except Exception as exc:
        print(f"BACKUP ERROR: {exc}")
        return 1
    print(f"BACKUP OK: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
