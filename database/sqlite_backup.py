from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from time import time

BASE_DIR = Path(__file__).resolve().parents[1]
DB_DIR = BASE_DIR / "database"
DEFAULT_DROPBOX_BACKUP_DIR = Path(
    os.getenv("LIVA_DROPBOX_BACKUP_DIR") or "/var/lib/liva/dropbox/Apps/Datenbanken"
)
DEFAULT_USB_AUTOMOUNT_ROOT = Path(
    os.getenv("LIVA_USB_AUTOMOUNT_ROOT") or "/mnt/liva-usb"
)
DEFAULT_RCLONE_REMOTE = (os.getenv("LIVA_RCLONE_BACKUP_REMOTE") or "dropbox:Apps/Datenbanken").strip()
DEFAULT_RCLONE_CONFIG = (
    Path(os.getenv("LIVA_RCLONE_CONFIG")).expanduser()
    if (os.getenv("LIVA_RCLONE_CONFIG") or "").strip()
    else Path("/var/lib/liva/.config/rclone/rclone.conf")
)
DEFAULT_MCP_WRITE_STATE = Path(
    os.getenv("LIVA_MCP_WRITE_STATE_BACKUP_SOURCE")
    or "/var/lib/liva/.local/share/liva-mcp-remote/liva_mcp_writes.sqlite3"
)
SQLITE_SUFFIXES = {".sqlite3", ".db"}
CANONICAL_DB_NAMES = (
    "training.sqlite3",
    "ernaehrung.sqlite3",
    "runs.sqlite3",
    "plans.sqlite3",
    "hrv.sqlite3",
    "auth.sqlite3",
    "core.sqlite3",
    "polar.sqlite3",
    "digital_activity.sqlite3",
    "control_center.sqlite3",
)


def _env_paths(name: str) -> list[Path]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for part in raw.split(","):
        candidate = Path(part.strip()).expanduser()
        key = str(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _path_key(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _mount_info_for_path(path: Path) -> tuple[Path, str] | None:
    mounts_file = Path("/proc/mounts")
    if not mounts_file.exists():
        return None
    path_key = _path_key(path)
    best: tuple[Path, str] | None = None
    best_len = -1
    for line in mounts_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mountpoint = Path(parts[1].replace("\\040", " "))
        mount_key = _path_key(mountpoint)
        if path_key != mount_key and not path_key.startswith(mount_key.rstrip("/") + "/"):
            continue
        if len(mount_key) > best_len:
            best = (mountpoint, str(parts[3] or ""))
            best_len = len(mount_key)
    return best


def _is_usb_backup_path(path: Path) -> bool:
    text = _path_key(path)
    return text.startswith("/mnt/") or text.startswith("/media/")


def _usb_target_preflight_error(path: Path) -> str:
    if not _is_usb_backup_path(path):
        return ""
    mount_info = _mount_info_for_path(path)
    if mount_info is None:
        return "USB target is not mounted"
    _mountpoint, options = mount_info
    option_set = {part.strip().lower() for part in options.split(",") if part.strip()}
    if "ro" in option_set:
        return "USB target is mounted read-only"
    return ""


def list_database_sources(db_dir: Path | None = None) -> dict[str, Path]:
    using_default_dir = db_dir is None
    db_dir = DB_DIR if db_dir is None else db_dir
    out: dict[str, Path] = {}
    if not db_dir.exists():
        return out
    for name in CANONICAL_DB_NAMES:
        src = db_dir / name
        if src.exists() and src.is_file():
            out[name] = src
    if using_default_dir and DEFAULT_MCP_WRITE_STATE.exists() and DEFAULT_MCP_WRITE_STATE.is_file():
        out[DEFAULT_MCP_WRITE_STATE.name] = DEFAULT_MCP_WRITE_STATE
    return out


def _mounted_usb_roots() -> list[Path]:
    roots: list[Path] = []
    mounts_file = Path("/proc/mounts")
    if not mounts_file.exists():
        return roots
    seen: set[str] = set()
    for line in mounts_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        source = str(parts[0] or "")
        mountpoint = Path(parts[1].replace("\\040", " "))
        mount_str = str(mountpoint)
        if not (mount_str.startswith("/mnt/") or mount_str.startswith("/media/")):
            continue
        if mount_str.endswith("/backups") or mount_str.endswith("/backups/sqlite"):
            continue
        if source.startswith("/dev/") and not _is_removable_block_device(source):
            continue
        key = _path_key(mountpoint)
        if key in seen:
            continue
        seen.add(key)
        roots.append(mountpoint)
    return roots


def _lsblk_devices() -> list[dict[str, object]]:
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,TRAN,RM,FSTYPE,LABEL,UUID,MOUNTPOINTS"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return []
    devices = payload.get("blockdevices")
    return devices if isinstance(devices, list) else []


def _sanitize_mount_name(raw: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw or "").strip()).strip("-._")
    return text or "usb"


def _automount_usb_partition(device_path: str, label: str = "", uuid: str = "") -> Path | None:
    if os.geteuid() != 0:
        return None
    mount_dir = DEFAULT_USB_AUTOMOUNT_ROOT / _sanitize_mount_name(label or uuid or Path(device_path).name)
    try:
        mount_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    result = subprocess.run(
        ["mount", device_path, str(mount_dir)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        return None
    return mount_dir


def _discover_usb_roots() -> list[Path]:
    roots = _mounted_usb_roots()
    seen = {_path_key(path) for path in roots}

    def add(root: Path | None) -> None:
        if root is None:
            return
        key = _path_key(root)
        if key in seen:
            return
        seen.add(key)
        roots.append(root)

    def walk(nodes: list[dict[str, object]], *, parent_is_usb: bool = False, parent_rm: bool = False) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            is_usb = parent_is_usb or str(node.get("tran") or "").strip().lower() == "usb"
            is_rm = parent_rm or bool(node.get("rm"))
            mountpoints = [
                Path(str(mp))
                for mp in (node.get("mountpoints") or [])
                if isinstance(mp, str) and str(mp).strip()
            ]
            for mountpoint in mountpoints:
                mount_str = str(mountpoint)
                if mount_str.startswith("/mnt/") or mount_str.startswith("/media/"):
                    add(mountpoint)

            children = node.get("children")
            if isinstance(children, list):
                walk(children, parent_is_usb=is_usb, parent_rm=is_rm)

            path = str(node.get("path") or "").strip()
            fstype = str(node.get("fstype") or "").strip()
            if mountpoints or not path or not fstype or not (is_usb and is_rm) or os.geteuid() != 0:
                continue
            add(_automount_usb_partition(path, str(node.get("label") or ""), str(node.get("uuid") or "")))

    walk(_lsblk_devices())
    return roots


def _is_removable_block_device(device_path: str) -> bool:
    match = re.match(r"^/dev/([a-zA-Z]+)", str(device_path or "").strip())
    if not match:
        return False
    removable_path = Path("/sys/block") / match.group(1) / "removable"
    try:
        return removable_path.read_text(encoding="utf-8").strip() == "1"
    except Exception:
        return False


def list_backup_targets() -> list[Path]:
    targets: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = _path_key(path)
        if key in seen:
            return
        seen.add(key)
        targets.append(path)

    add(DEFAULT_DROPBOX_BACKUP_DIR)

    explicit_dirs = _env_paths("LIVA_USB_BACKUP_DIRS")
    if explicit_dirs:
        for path in explicit_dirs:
            add(path)
        return targets

    explicit_roots = _env_paths("LIVA_USB_MOUNT_ROOTS")
    if explicit_roots:
        for root in explicit_roots:
            add(root / "backups" / "sqlite")
        return targets

    for root in _discover_usb_roots():
        add(root / "backups" / "sqlite")
    return targets


def backup_sqlite_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_name(f"{dst.name}.tmp")
    if tmp_dst.exists():
        tmp_dst.unlink()

    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    dst_conn = sqlite3.connect(tmp_dst, timeout=30, check_same_thread=False)
    try:
        src_conn.execute("PRAGMA busy_timeout = 30000")
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()

    os.replace(tmp_dst, dst)


def _prune_target_dir(target: Path, keep_names: set[str]) -> None:
    if not target.exists():
        return
    for path in target.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in SQLITE_SUFFIXES:
            continue
        if path.name in keep_names:
            continue
        path.unlink()


def _sync_dir_to_rclone_remote(local_dir: Path, remote: str) -> dict[str, object]:
    if not remote:
        return {"ok": False, "status": "skipped", "reason": "no_remote"}
    cmd = ["rclone", "sync", str(local_dir), remote, "--create-empty-src-dirs"]
    if DEFAULT_RCLONE_CONFIG.exists() and DEFAULT_RCLONE_CONFIG.is_file():
        cmd.extend(["--config", str(DEFAULT_RCLONE_CONFIG)])
    try:
        env = os.environ.copy()
        env.setdefault("HOME", "/var/lib/liva")
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except FileNotFoundError:
        return {"ok": False, "status": "skipped", "reason": "rclone_missing"}
    except Exception as exc:
        return {"ok": False, "status": "error", "reason": str(exc)}

    if result.returncode != 0:
        return {
            "ok": False,
            "status": "error",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    return {"ok": True, "status": "ok"}


def run_backup(
    source_files: dict[str, Path] | None = None,
    target_dirs: list[Path] | None = None,
) -> dict[str, object]:
    sources = source_files or list_database_sources()
    targets = target_dirs or list_backup_targets()
    started_at = int(time())
    results: dict[str, dict[str, str]] = {}
    keep_names = set(sources)
    target_errors: dict[str, dict[str, str]] = {}

    for target in targets:
        target_key = str(target)
        per_target: dict[str, str] = {}
        results[target_key] = per_target
        try:
            preflight_error = _usb_target_preflight_error(target)
            if preflight_error:
                raise RuntimeError(preflight_error)
            target.mkdir(parents=True, exist_ok=True)
            _prune_target_dir(target, keep_names)
        except Exception as exc:
            results[target_key] = {"_target": f"error: {exc}"}
            target_errors[target_key] = dict(results[target_key])
            continue
        for filename, src in sources.items():
            try:
                backup_sqlite_file(src, target / filename)
                per_target[filename] = "ok"
            except Exception as exc:
                per_target[filename] = f"error: {exc}"
                target_errors.setdefault(target_key, {})[filename] = per_target[filename]

    remote_sync = _sync_dir_to_rclone_remote(DEFAULT_DROPBOX_BACKUP_DIR, DEFAULT_RCLONE_REMOTE)

    return {
        "ok": not target_errors,
        "started_at": started_at,
        "sources": {name: str(path) for name, path in sources.items()},
        "targets": results,
        "target_errors": target_errors,
        "remote_sync": remote_sync,
    }


def latest_backup_timestamp(target_dirs: list[Path] | None = None) -> float | None:
    latest: float | None = None
    for target in (target_dirs or list_backup_targets()):
        try:
            files = [path for path in target.iterdir() if path.is_file() and path.suffix.lower() in SQLITE_SUFFIXES]
        except Exception:
            continue
        for file_path in files:
            try:
                mtime = float(file_path.stat().st_mtime)
            except Exception:
                continue
            latest = mtime if latest is None else max(latest, mtime)
    return latest


if __name__ == "__main__":
    print(json.dumps(run_backup(), ensure_ascii=True))
