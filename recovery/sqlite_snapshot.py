from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path


class SnapshotError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_snapshot(source: Path, staging: Path) -> tuple[Path, str]:
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    snapshot = staging / f"{source.name}.snapshot"
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    target_conn = sqlite3.connect(snapshot, timeout=30, check_same_thread=False)
    try:
        source_conn.execute("PRAGMA busy_timeout=30000")
        source_conn.backup(target_conn)
        target_conn.commit()
    except Exception as exc:
        raise SnapshotError(f"online snapshot failed for {source.name}: {type(exc).__name__}") from exc
    finally:
        target_conn.close()
        source_conn.close()
    os.chmod(snapshot, 0o600)
    check_conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        result = str(check_conn.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        check_conn.close()
    if result.lower() != "ok":
        snapshot.unlink(missing_ok=True)
        raise SnapshotError(f"quick_check failed for {source.name}: {result}")
    return snapshot, result

