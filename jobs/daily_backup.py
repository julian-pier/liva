from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from database.sqlite_backup import run_backup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_META_PATH = PROJECT_ROOT / ".backup" / "last_backup.json"
CODE_SNAPSHOT_REMOTE_REF = "refs/heads/main"
CODE_SNAPSHOT_EXCLUDES = (
    ".backup",
    "backups",
    "backup-staging",
    "data",
    "import",
    "logs",
    "output",
    "uploads",
    "var",
    "strava_sync/data",
    "smart_home/data",
    ":(glob).env*",
    ":(glob).incident-backup-*",
    ":(glob)**/*.sqlite",
    ":(glob)**/*.sqlite3",
    ":(glob)**/*.sqlite3.*",
    ":(glob)**/*.db",
    ":(glob)**/*.db.*",
    ":(glob)**/*.log",
    ":(glob)**/*.log.*",
    ":(glob)**/*.out",
)


def _truthy_env(name: str, default: str = "0") -> bool:
    return (os.getenv(name) or default).strip().lower() in {"1", "true", "yes", "on"}


def _run_git(args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(extra_env or {})
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env["HOME"] = "/var/lib/liva"
    repo_safe_dir = str(PROJECT_ROOT)
    base_cmd = ["git", "-c", f"safe.directory={repo_safe_dir}", *args]
    if os.geteuid() == 0:
        runuser_bin = shutil.which("runuser")
        if runuser_bin:
            base_cmd = [runuser_bin, "-u", "liva", "--", *base_cmd]
    return subprocess.run(
        base_cmd,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_git_with_env(args: list[str], extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run_git(args, extra_env=extra_env)


def _git_error(step: str, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return {
        "ok": False,
        "status": "error",
        "step": step,
        "stderr": result.stderr.strip(),
        "stdout": result.stdout.strip(),
    }


def _audit_snapshot_tree(tree_sha: str, git_env: dict[str, str]) -> tuple[int, list[str]]:
    listing = _run_git_with_env(["ls-tree", "-r", "-l", tree_sha], git_env)
    if listing.returncode != 0:
        return 0, ["snapshot tree could not be inspected"]

    unsafe: list[str] = []
    file_count = 0
    forbidden_prefixes = (".backup/", "backups/", "data/", "import/", "logs/", "output/", "uploads/", "var/", "strava_sync/data/", "smart_home/data/")
    for line in listing.stdout.splitlines():
        metadata, separator, path = line.partition("\t")
        if not separator:
            continue
        parts = metadata.split()
        if len(parts) < 4 or parts[1] != "blob":
            continue
        file_count += 1
        try:
            size = int(parts[3])
        except (TypeError, ValueError):
            size = 0
        lowered = path.lower()
        forbidden_db = lowered.endswith((".sqlite", ".sqlite3", ".db", "-wal", "-shm", "-journal")) or ".sqlite3." in lowered
        if lowered.startswith(forbidden_prefixes) or forbidden_db or size >= 50 * 1024 * 1024:
            unsafe.append(path)
    return file_count, unsafe


def _write_backup_metadata(payload: dict[str, object]) -> None:
    BACKUP_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    BACKUP_META_PATH.write_text(json.dumps(snapshot, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def load_backup_metadata() -> dict[str, object] | None:
    if not BACKUP_META_PATH.exists():
        return None
    try:
        raw = json.loads(BACKUP_META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def push_code_snapshot(backup_payload: dict[str, object] | None = None, *, dry_run: bool = False) -> dict[str, object]:
    """Publish a sanitized source snapshot without touching the user's branch or index."""
    if not (PROJECT_ROOT / ".git").exists():
        return {"ok": True, "status": "skipped", "reason": "not_a_git_repo"}

    origin = _run_git(["remote", "get-url", "origin"])
    if origin.returncode != 0:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "missing_origin",
            "stderr": origin.stderr.strip(),
            "stdout": origin.stdout.strip(),
        }

    fetch = _run_git(["fetch", "--no-tags", "origin", f"+{CODE_SNAPSHOT_REMOTE_REF}:refs/remotes/origin/main"])
    if fetch.returncode != 0:
        return _git_error("git_fetch", fetch)

    remote_head = _run_git(["rev-parse", "--verify", "refs/remotes/origin/main"])
    if remote_head.returncode != 0:
        return _git_error("resolve_remote_main", remote_head)
    remote_sha = remote_head.stdout.strip()

    index_path = PROJECT_ROOT / ".git" / f"nightly-source-snapshot-{os.getpid()}.index"
    index_lock = Path(f"{index_path}.lock")
    git_env = {"GIT_INDEX_FILE": str(index_path)}
    try:
        index_path.unlink(missing_ok=True)
        index_lock.unlink(missing_ok=True)

        read_tree = _run_git_with_env(["read-tree", "HEAD"], git_env)
        if read_tree.returncode != 0:
            return _git_error("snapshot_read_tree", read_tree)

        add = _run_git_with_env(["add", "-A", "--", "."], git_env)
        if add.returncode != 0:
            return _git_error("snapshot_add", add)

        remove_data = _run_git_with_env(
            ["rm", "-r", "--cached", "--ignore-unmatch", "--", *CODE_SNAPSHOT_EXCLUDES],
            git_env,
        )
        if remove_data.returncode != 0:
            return _git_error("snapshot_remove_runtime_data", remove_data)

        write_tree = _run_git_with_env(["write-tree"], git_env)
        if write_tree.returncode != 0:
            return _git_error("snapshot_write_tree", write_tree)
        tree_sha = write_tree.stdout.strip()

        file_count, unsafe_paths = _audit_snapshot_tree(tree_sha, git_env)
        if unsafe_paths:
            return {
                "ok": False,
                "status": "error",
                "step": "snapshot_audit",
                "unsafe_paths": unsafe_paths,
            }

        remote_tree = _run_git(["rev-parse", f"{remote_sha}^{{tree}}"])
        if remote_tree.returncode != 0:
            return _git_error("resolve_remote_tree", remote_tree)
        if remote_tree.stdout.strip() == tree_sha:
            return {
                "ok": True,
                "status": "unchanged",
                "remote_ref": CODE_SNAPSHOT_REMOTE_REF,
                "remote_head": remote_sha,
                "snapshot_files": file_count,
            }

        status_res = _run_git(["status", "--porcelain"])
        prepared = {
            "ok": True,
            "status": "prepared" if dry_run else "ok",
            "remote_ref": CODE_SNAPSHOT_REMOTE_REF,
            "parent": remote_sha,
            "snapshot_tree": tree_sha,
            "snapshot_files": file_count,
            "working_tree_dirty": bool(status_res.stdout.strip()) if status_res.returncode == 0 else None,
        }
        if dry_run:
            return prepared

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        commit = _run_git_with_env(
            ["commit-tree", tree_sha, "-p", remote_sha, "-m", f"nightly source snapshot {stamp}"],
            git_env,
        )
        if commit.returncode != 0:
            return _git_error("snapshot_commit", commit)
        snapshot_sha = commit.stdout.strip()

        push = _run_git(["push", "origin", f"{snapshot_sha}:{CODE_SNAPSHOT_REMOTE_REF}"])
        if push.returncode != 0:
            error = _git_error("git_push", push)
            error["snapshot_head"] = snapshot_sha
            return error
        prepared["snapshot_head"] = snapshot_sha
        return prepared
    finally:
        index_path.unlink(missing_ok=True)
        index_lock.unlink(missing_ok=True)


def run_daily_backup() -> dict[str, object]:
    sqlite_backup = run_backup()
    code_snapshot_enabled = _truthy_env("LIVA_CODE_SNAPSHOT_ENABLED")
    code_push = (
        push_code_snapshot({"sqlite_backup": sqlite_backup})
        if code_snapshot_enabled
        else {"ok": True, "status": "skipped", "reason": "disabled"}
    )
    _write_backup_metadata({"sqlite_backup": sqlite_backup, "code_push": code_push})
    sqlite_ok = bool(sqlite_backup.get("ok")) or (
        not _usb_backup_required() and _only_usb_target_errors(sqlite_backup)
    )
    remote_sync_ok = bool((sqlite_backup.get("remote_sync") or {}).get("ok", True))
    code_push_required = _truthy_env("LIVA_CODE_PUSH_REQUIRED")
    code_push_ok = bool(code_push.get("ok")) or not code_push_required
    ok = code_push_ok and sqlite_ok and remote_sync_ok
    return {
        "ok": ok,
        "code_push": code_push,
        "sqlite_backup": sqlite_backup,
    }


def _usb_backup_required() -> bool:
    return _truthy_env("LIVA_USB_BACKUP_REQUIRED")


def _only_usb_target_errors(sqlite_backup: dict[str, object]) -> bool:
    target_errors = sqlite_backup.get("target_errors")
    if not isinstance(target_errors, dict) or not target_errors:
        return False
    for path in target_errors:
        text = str(path)
        if "/mnt/" not in text and "/media/" not in text:
            return False
    return True


def exit_code_for_result(result: dict[str, object]) -> int:
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    result = run_daily_backup()
    print(json.dumps(result, ensure_ascii=True))
    sys.exit(exit_code_for_result(result))
