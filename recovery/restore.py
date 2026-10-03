from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RestoreError(RuntimeError):
    pass


@dataclass
class RestorePlan:
    backup_id: str
    staging: Path
    manifest: dict[str, Any]
    entries: list[dict[str, Any]]


def _safe_extract(stream: tarfile.TarFile, staging: Path) -> None:
    staging_resolved = staging.resolve()
    for member in stream:
        if not member.isfile() and not member.isdir():
            raise RestoreError("archive contains unsupported link or special file")
        target = (staging / member.name).resolve()
        if not target.is_relative_to(staging_resolved):
            raise RestoreError("archive path escapes staging")
        stream.extract(member, staging, set_attrs=False, numeric_owner=False, filter="data")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def plan_restore(*, bundle: Path, identity: Path, staging: Path) -> RestorePlan:
    sidecar = bundle.with_name(bundle.name + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text(encoding="utf-8").split(maxsplit=1)[0]
        if expected != _digest(bundle):
            raise RestoreError("ciphertext sidecar hash mismatch")
    receipt = bundle.with_name(bundle.name + ".receipt.json")
    if receipt.exists():
        try:
            receipt_hash = str(json.loads(receipt.read_text(encoding="utf-8"))["ciphertext_sha256"])
        except Exception as exc:
            raise RestoreError("receipt is invalid") from exc
        if receipt_hash != _digest(bundle):
            raise RestoreError("receipt hash mismatch")
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    age = subprocess.Popen(["age", "-d", "-i", str(identity), str(bundle)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    zstd = subprocess.Popen(["zstd", "-q", "-d", "-c"], stdin=age.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert age.stdout and zstd.stdout
    age.stdout.close()
    try:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as archive:
            _safe_extract(archive, staging)
    except (tarfile.TarError, OSError) as exc:
        raise RestoreError("decryption or decompression produced an invalid archive") from exc
    finally:
        zstd.stdout.close()
    zstd_rc = zstd.wait(); age_rc = age.wait()
    if zstd_rc or age_rc:
        raise RestoreError("decryption or decompression failed")
    manifest_path = staging / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RestoreError("manifest is unavailable") from exc
    entries = list(manifest.get("entries") or [])
    for item in entries:
        path = staging / str(item["archive_path"])
        if not path.is_file() or _digest(path) != item.get("sha256"):
            raise RestoreError(f"manifest hash mismatch: {item.get('archive_path')}")
        if item.get("sqlite_snapshot"):
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
            if str(result).lower() != "ok":
                raise RestoreError(f"sqlite integrity check failed: {item.get('archive_path')}")
    return RestorePlan(str(manifest["backup_id"]), staging, manifest, entries)


def apply_restore(plan: RestorePlan, *, target_root: Path, confirmation: str | None = None, include_manual: set[str] | None = None) -> list[str]:
    include_manual = include_manual or set()
    if target_root.resolve() == Path("/") and confirmation != f"RESTORE {plan.backup_id} TO /":
        raise RestoreError("exact root restore confirmation is required")
    restored: list[str] = []
    for item in sorted(plan.entries, key=lambda entry: 0 if entry.get("restore_policy") == "bootstrap" else 1):
        category = str(item.get("category"))
        policy = str(item.get("restore_policy"))
        if policy == "manual" and category not in include_manual:
            continue
        if policy == "manual" and category == "tailscale_identity" and "tailscale_identity" not in include_manual:
            continue
        source = plan.staging / str(item["archive_path"])
        relative = str(item["restore_path"]).lstrip("/")
        destination = target_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".restore-partial")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, int(item["mode"]))
        try:
            os.chown(temporary, int(item["uid"]), int(item["gid"]))
        except PermissionError:
            pass
        os.replace(temporary, destination)
        if item.get("sqlite_snapshot"):
            connection = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                connection.close()
            if str(result).lower() != "ok":
                raise RestoreError(f"restored sqlite integrity check failed: {destination}")
        restored.append(str(destination))
    return restored
