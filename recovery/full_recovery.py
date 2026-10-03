from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import uuid
from pathlib import Path
from typing import Any, Iterable

from .crypto import CryptoError, age_binary, recipient_fingerprint
from .manifest import archive_name, base_manifest, encode, git_reference, utc_now
from .policy import RecoveryPolicy, RecoverySource, production_policy
from .restore import apply_restore, plan_restore
from .retention import prune
from .sqlite_snapshot import SnapshotError, create_snapshot, sha256_file
from .storage import free_bytes, inspect_device, safe_target, validate_device


DEFAULT_CONFIG = Path("/etc/liva/recovery/config.toml")


class RecoveryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RecoveryError("NOT_INITIALIZED", "recovery config is missing") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RecoveryError("NOT_INITIALIZED", "recovery config is unavailable or invalid") from exc
    for section in ("recovery", "usb", "crypto"):
        if not isinstance(value.get(section), dict):
            raise RecoveryError("NOT_INITIALIZED", f"config section {section} is missing")
    return value


def preflight(*, config_path: Path = DEFAULT_CONFIG, policy: RecoveryPolicy | None = None) -> dict[str, Any]:
    """Read-only. This is deliberately the only production-safe default action."""
    policy = policy or production_policy()
    try:
        config = load_config(config_path)
    except RecoveryError as exc:
        return {"ok": False, "mode": "read_only", "error_code": exc.code, "message": str(exc), "inventory_drift": policy.inventory_drift()}
    recipients = Path(str(config["crypto"].get("recipients_file") or ""))
    try:
        fingerprint = recipient_fingerprint(recipients)
    except CryptoError as exc:
        return {"ok": False, "mode": "read_only", "error_code": "NOT_INITIALIZED", "message": str(exc)}
    if not fingerprint:
        return {"ok": False, "mode": "read_only", "error_code": "NOT_INITIALIZED", "message": "recipient is not configured", "inventory_drift": policy.inventory_drift()}
    try:
        policy.present_sources()
    except FileNotFoundError as exc:
        return {"ok": False, "mode": "read_only", "error_code": "INVENTORY_DRIFT", "message": str(exc), "inventory_drift": []}
    drift = policy.inventory_drift()
    if drift:
        return {"ok": False, "mode": "read_only", "error_code": "INVENTORY_DRIFT", "inventory_drift": drift, "recipient_fingerprint": fingerprint}
    usb = config["usb"]
    observed = inspect_device(uuid=str(usb.get("uuid") or ""), label=str(usb.get("label") or ""), filesystem=str(usb.get("filesystem") or ""), min_capacity_bytes=int(usb.get("min_capacity_bytes") or 0))
    device_error = validate_device(observed, uuid=str(usb.get("uuid") or ""), label=str(usb.get("label") or ""), filesystem=str(usb.get("filesystem") or ""), min_capacity_bytes=int(usb.get("min_capacity_bytes") or 0), required_mount_options=tuple(str(item) for item in usb.get("mount_options", ())))
    if not age_binary():
        return {"ok": False, "mode": "read_only", "error_code": "NOT_INITIALIZED", "message": "age is not installed", "device": observed.to_dict()}
    return {"ok": device_error is None and bool(fingerprint), "mode": "read_only", "error_code": device_error, "recipient_fingerprint": fingerprint, "device": observed.to_dict(), "inventory_drift": []}


def _excluded(relative: Path, excludes: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(part, pattern) or fnmatch.fnmatch(str(relative), pattern) for pattern in excludes for part in relative.parts)


def _files(source: RecoverySource) -> Iterable[Path]:
    if source.kind != "tree":
        if source.path.is_file():
            yield source.path
        return
    for item in sorted(source.path.rglob("*")):
        if item.is_file() and not _excluded(item.relative_to(source.path), source.excludes):
            yield item


def _archive_path(path: Path) -> str:
    return "payload/" + str(path).lstrip("/")


def _entry(source: RecoverySource, original: Path, archived: str, digest: str, sqlite_snapshot: bool, sqlite_check: str | None, archived_size: int | None = None) -> dict[str, Any]:
    metadata = original.stat()
    return {"category": source.category, "archive_path": archived, "restore_path": str(original), "restore_policy": source.restore_policy, "type": "file", "size": archived_size if archived_size is not None else metadata.st_size, "sha256": digest, "uid": metadata.st_uid, "gid": metadata.st_gid, "mode": stat.S_IMODE(metadata.st_mode), "mtime": metadata.st_mtime, "sqlite_snapshot": sqlite_snapshot, "sqlite_snapshot_method": "sqlite_online_backup_api" if sqlite_snapshot else None, "sqlite_check_result": sqlite_check}


def _add_file(archive: tarfile.TarFile, *, read_path: Path, original: Path, name: str) -> None:
    info = archive.gettarinfo(str(original), arcname=name)
    info.size = read_path.stat().st_size
    with read_path.open("rb") as handle:
        archive.addfile(info, handle)


def _container_images() -> list[str]:
    try:
        result = subprocess.run(["docker", "ps", "--format", "{{.Image}}@{{.ID}}"], text=True, capture_output=True, timeout=5, check=False)
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    except OSError:
        return []


def _source_bytes(policy: RecoveryPolicy) -> int:
    return sum(path.stat().st_size for item in policy.present_sources() for path in _files(item) if item.kind != "sqlite") + sum(item.path.stat().st_size for item in policy.present_sources() if item.kind == "sqlite")


def create_backup(*, config_path: Path, target_dir: Path, policy: RecoveryPolicy, project_root: Path | None = None) -> dict[str, Any]:
    """Create an encrypted bundle for an explicitly supplied, already-safe target.

    Production callers must perform device preflight and mount lifecycle outside this
    function. Tests use a temporary directory; this function never discovers devices.
    """
    config = load_config(config_path)
    recipients = Path(str(config["crypto"].get("recipients_file") or ""))
    fingerprint = recipient_fingerprint(recipients)
    if not fingerprint:
        raise RecoveryError("NOT_INITIALIZED", "recipient is not configured")
    drift = policy.inventory_drift()
    if drift:
        raise RecoveryError("INVENTORY_DRIFT", ", ".join(drift))
    estimated_size = _source_bytes(policy)
    fat_max = int(config["usb"].get("fat_max_file_bytes") or (4 * 1024**3 - 1))
    if estimated_size > fat_max:
        raise RecoveryError("FAT_FILE_LIMIT", "estimated encrypted archive exceeds FAT32 file limit")
    if target_dir.exists() and free_bytes(target_dir) < estimated_size:
        raise RecoveryError("SPACE_LOW", "target does not have enough free space")
    backup_id = uuid.uuid4().hex
    name = archive_name(backup_id)
    final = safe_target(target_dir, name)
    partial = final.with_name(final.name + ".partial")
    staging_root = Path(str(config["recovery"].get("staging_root") or tempfile.gettempdir()))
    staging = staging_root / f"liva-recovery-{backup_id}"
    root = project_root or Path.cwd()
    source = git_reference(root)
    warnings = [source["warning"]] if source.get("warning") else []
    manifest = base_manifest(backup_id=backup_id, recipient_fingerprint=fingerprint, source_commit=source, warnings=warnings)
    manifest["excludes"] = sorted({value for item in policy.sources for value in item.excludes})
    manifest["container_images"] = _container_images()
    seen_archive_paths: set[str] = set()
    age = subprocess.Popen([age_binary() or "age", "-R", str(recipients), "-o", str(partial)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    zstd = subprocess.Popen(["zstd", "-q", "-6", "-T1", "-c"], stdin=subprocess.PIPE, stdout=age.stdin, stderr=subprocess.PIPE)
    assert zstd.stdin and age.stdin
    age.stdin.close()
    try:
        with tarfile.open(fileobj=zstd.stdin, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for item in policy.present_sources():
                if item.kind == "sqlite":
                    snapshot, check = create_snapshot(item.path, staging)
                    try:
                        digest = sha256_file(snapshot)
                        name_in_archive = _archive_path(item.path)
                        if name_in_archive in seen_archive_paths:
                            raise RecoveryError("INVENTORY_DRIFT", f"duplicate archive path: {name_in_archive}")
                        seen_archive_paths.add(name_in_archive)
                        _add_file(archive, read_path=snapshot, original=item.path, name=name_in_archive)
                        entry = _entry(item, item.path, name_in_archive, digest, True, check, snapshot.stat().st_size)
                        manifest["entries"].append(entry)
                    finally:
                        snapshot.unlink(missing_ok=True)
                    continue
                for path in _files(item):
                    digest = sha256_file(path)
                    name_in_archive = _archive_path(path)
                    if name_in_archive in seen_archive_paths:
                        raise RecoveryError("INVENTORY_DRIFT", f"duplicate archive path: {name_in_archive}")
                    seen_archive_paths.add(name_in_archive)
                    _add_file(archive, read_path=path, original=path, name=name_in_archive)
                    manifest["entries"].append(_entry(item, path, name_in_archive, digest, False, None))
            manifest["total_size"] = sum(int(item["size"]) for item in manifest["entries"])
            manifest["file_count"] = len(manifest["entries"])
            data = encode(manifest)
            info = tarfile.TarInfo("manifest.json"); info.size = len(data); info.mode = 0o600
            import io
            archive.addfile(info, io.BytesIO(data))
        zstd.stdin.close()
        zstd_rc = zstd.wait(); age_rc = age.wait()
        if zstd_rc or age_rc:
            raise RecoveryError("PIPELINE_FAILED", "compression or encryption failed")
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        expected_hash = sha256_file(partial)
        os.replace(partial, final)
        actual_hash = sha256_file(final)
        if actual_hash != expected_hash:
            raise RecoveryError("HASH_MISMATCH", "ciphertext reread did not match")
        finished_at = utc_now()
        receipt = {"backup_id": backup_id, "format_version": "LIVA-FR-v1", "finished_at": finished_at, "ciphertext_size": final.stat().st_size, "ciphertext_sha256": actual_hash, "recipient_fingerprint": fingerprint, "status": "transport_verified", "transport_verification": "ciphertext_sha256_reread", "source_commit": source.get("source_commit"), "restore_tested": False}
        receipt_path = final.with_name(final.name + ".receipt.json")
        hash_path = final.with_name(final.name + ".sha256")
        for path, data in ((receipt_path, json.dumps(receipt, sort_keys=True) + "\n"), (hash_path, actual_hash + "  " + final.name + "\n")):
            temporary = path.with_name(path.name + ".partial")
            temporary.write_text(data, encoding="utf-8")
            with temporary.open("rb") as handle: os.fsync(handle.fileno())
            os.replace(temporary, path)
        prune(target_dir)
        return {"ok": True, "backup_id": backup_id, "bundle": str(final), "receipt": receipt, "manifest": manifest}
    except SnapshotError as exc:
        raise RecoveryError("SQLITE_CHECK_FAILED", str(exc)) from exc
    except (BrokenPipeError, OSError, tarfile.TarError) as exc:
        raise RecoveryError("PIPELINE_FAILED", "backup pipeline stopped before verification") from exc
    finally:
        try:
            if zstd.stdin and not zstd.stdin.closed: zstd.stdin.close()
        except Exception: pass
        for process in (zstd, age):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        partial.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recovery.full_recovery")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("preflight"); pre.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    create = sub.add_parser("create"); create.add_argument("--config", type=Path, default=DEFAULT_CONFIG); create.add_argument("--target", type=Path, required=True)
    restore = sub.add_parser("restore"); restore.add_argument("--bundle", type=Path, required=True); restore.add_argument("--identity", type=Path, required=True); restore.add_argument("--target-root", type=Path, required=True); restore.add_argument("--staging", type=Path, required=True); restore.add_argument("--plan", action="store_true"); restore.add_argument("--apply", action="store_true"); restore.add_argument("--confirmation"); restore.add_argument("--include-manual", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result = preflight(config_path=args.config)
        print(json.dumps(result, ensure_ascii=True)); return 0 if result["ok"] else 2
    if args.command == "create":
        result = preflight(config_path=args.config)
        if not result["ok"]:
            print(json.dumps(result, ensure_ascii=True)); return 2
        print(json.dumps(create_backup(config_path=args.config, target_dir=args.target, policy=production_policy()), ensure_ascii=True)); return 0
    if not args.plan and not args.apply:
        parser.error("restore requires --plan or --apply")
    plan = plan_restore(bundle=args.bundle, identity=args.identity, staging=args.staging)
    output: dict[str, Any] = {"ok": True, "backup_id": plan.backup_id, "planned_entries": len(plan.entries), "manual_categories": sorted({str(item["category"]) for item in plan.entries if item["restore_policy"] == "manual"})}
    if args.apply:
        output["restored"] = apply_restore(plan, target_root=args.target_root, confirmation=args.confirmation, include_manual=set(args.include_manual))
    print(json.dumps(output, ensure_ascii=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
