"""Controlled systemd entry points for encrypted Full Recovery."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from control_center import store

from .full_recovery import DEFAULT_CONFIG, RecoveryError, create_backup, load_config, preflight
from .policy import production_policy


LOCK_PATH = Path("/var/lib/liva-recovery/full-recovery.lock")


@contextmanager
def exclusive_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """Refuse overlapping manual, timer, and catch-up backup runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError("RECOVERY_LOCKED", "another full recovery run is active") from exc
        yield
    finally:
        os.close(descriptor)


def _observed(result: dict[str, Any]) -> dict[str, Any]:
    value = result.get("device")
    return value if isinstance(value, dict) else {}


def _expected(config_path: Path) -> dict[str, Any]:
    usb = load_config(config_path)["usb"]
    return {key: usb.get(key) for key in ("uuid", "label", "filesystem")}


def _record_failure(*, result: dict[str, Any], code: str, message: str) -> None:
    store.set_full_recovery_state(
        {
            "phase": "failed",
            "status": "failed",
            "backup_due": True,
            "error_code": code,
            "message": message,
            "observed_device": _observed(result),
        }
    )


def run_backup(*, config_path: Path = DEFAULT_CONFIG, target_dir: Path | None = None) -> dict[str, Any]:
    """Run exactly one preflighted, locked, transport-verified backup."""
    result = preflight(config_path=config_path)
    if not result.get("ok"):
        code = str(result.get("error_code") or "PREFLIGHT_FAILED")
        message = str(result.get("message") or code)
        _record_failure(result=result, code=code, message=message)
        raise RecoveryError(code, message)
    config = load_config(config_path)
    target = target_dir or Path("/mnt/liva-recovery/backups/full-recovery")
    started = time.monotonic()
    with exclusive_lock():
        created = create_backup(config_path=config_path, target_dir=target, policy=production_policy())
    receipt = created["receipt"]
    now = str(receipt["finished_at"])
    expected = _expected(config_path)
    store.set_full_recovery_state(
        {
            "phase": "verified",
            "status": "verified",
            "backup_due": False,
            "due_since": None,
            "next_retry_at": None,
            "last_attempt_at": now,
            "last_created_at": now,
            "last_transport_verified_at": now,
            "backup_id": created["backup_id"],
            "expected_device": expected,
            "observed_device": _observed(result),
            "primary_media_uuid": expected.get("uuid"),
            "primary_media_status": "transport_verified",
            "verification_state": "transport_verified",
            "recipient_fingerprint": receipt["recipient_fingerprint"],
            "file_count": created["manifest"]["file_count"],
            "category_count": len({entry["category"] for entry in created["manifest"]["entries"]}),
            "source_bytes": created["manifest"]["total_size"],
            "ciphertext_bytes": receipt["ciphertext_size"],
            "runtime_seconds": round(time.monotonic() - started, 3),
            "retention_deletions": [],
            "source_commit": receipt.get("source_commit"),
            "error_code": None,
            "message": None,
        }
    )
    return {"ok": True, "mode": "created", "backup_id": created["backup_id"], "receipt": receipt}


def run_catchup(*, config_path: Path = DEFAULT_CONFIG, target_dir: Path | None = None) -> dict[str, Any]:
    """Create only when an explicitly persisted due state requires it."""
    result = preflight(config_path=config_path)
    if not result.get("ok"):
        code = str(result.get("error_code") or "PREFLIGHT_FAILED")
        message = str(result.get("message") or code)
        _record_failure(result=result, code=code, message=message)
        raise RecoveryError(code, message)
    state = store.get_full_recovery_state() or {}
    if not bool(state.get("backup_due")):
        return {"ok": True, "mode": "noop", "reason": "backup_not_due", "backup_id": state.get("backup_id")}
    return run_backup(config_path=config_path, target_dir=target_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recovery.automation")
    parser.add_argument("command", choices=("backup", "catchup"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args(argv)
    try:
        output = run_backup(config_path=args.config, target_dir=args.target) if args.command == "backup" else run_catchup(config_path=args.config, target_dir=args.target)
    except RecoveryError as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "message": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
