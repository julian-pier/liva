from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeviceObservation:
    present: bool
    path: str | None = None
    uuid: str | None = None
    label: str | None = None
    filesystem: str | None = None
    removable: bool = False
    size: int | None = None
    mounted: bool = False
    read_only: bool | None = None
    dirty: bool | None = None
    mount_options: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class MountLifecycle:
    """Explicit mutation boundary for 006C; never used by read-only preflight."""

    mountpoint: Path
    mount_options: tuple[str, ...]

    def mount_for_create(self, observation: DeviceObservation, *, expected: dict[str, Any], runner=subprocess.run) -> None:
        error = validate_device(observation, uuid=str(expected["uuid"]), label=str(expected["label"]), filesystem=str(expected["filesystem"]), min_capacity_bytes=int(expected["min_capacity_bytes"]))
        if error:
            raise RuntimeError(error)
        if not observation.path:
            raise RuntimeError("USB_MISSING")
        self.mountpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = runner(["mount", "-o", ",".join(self.mount_options), observation.path, str(self.mountpoint)], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("MOUNT_FAILED")

    def unmount_after_create(self, *, runner=subprocess.run) -> None:
        result = runner(["umount", str(self.mountpoint)], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("UNMOUNT_FAILED")


def _walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for node in nodes:
        output.append(node)
        output.extend(_walk(node.get("children") or []))
    return output


def inspect_device(*, uuid: str, label: str, filesystem: str, min_capacity_bytes: int, runner=subprocess.run) -> DeviceObservation:
    """Read-only inspection only; this function never mounts, labels, or repairs."""
    try:
        result = runner(["lsblk", "-J", "-b", "-o", "PATH,UUID,LABEL,FSTYPE,RM,SIZE,MOUNTPOINTS,RO"], capture_output=True, text=True, timeout=10, check=False)
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return DeviceObservation(False)
    for node in _walk(payload.get("blockdevices") or []):
        if str(node.get("uuid") or "") != uuid:
            continue
        mounts = [value for value in node.get("mountpoints") or [] if value]
        path = str(node.get("path") or "") or None
        mount_options = _mount_options(path, runner) if mounts and path else ()
        # FAT sets its dirty bit while mounted RW. Running fsck against a mounted
        # filesystem is both misleading and unsafe; validate its mount policy instead.
        dirty = _fat_dirty(path, runner) if path and not mounts and str(node.get("fstype") or "").lower() in {"vfat", "fat", "msdos"} else None
        observed = DeviceObservation(True, path, str(node.get("uuid") or "") or None, str(node.get("label") or "") or None, str(node.get("fstype") or "") or None, bool(node.get("rm")), int(node.get("size") or 0), bool(mounts), bool(node.get("ro")), dirty, mount_options)
        return observed
    return DeviceObservation(False)


def _fat_dirty(path: str, runner: Any) -> bool | None:
    """Probe FAT metadata without changing it. Unknown is intentionally not dirty."""
    for program in ("fsck.fat", "fsck.vfat"):
        try:
            result = runner([program, "-n", "-v", path], capture_output=True, text=True, timeout=15, check=False)
        except FileNotFoundError:
            continue
        except Exception:
            return None
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        lowered = output.lower()
        return "dirty bit is set" in lowered or "filesystem has been changed" in lowered or "fat differs" in lowered
    return None


def _mount_options(path: str, runner: Any) -> tuple[str, ...]:
    try:
        result = runner(["findmnt", "-no", "OPTIONS", path], capture_output=True, text=True, timeout=5, check=False)
        if result.returncode:
            return ()
        return tuple(part.strip() for part in (result.stdout or "").strip().split(",") if part.strip())
    except Exception:
        return ()


def validate_device(observed: DeviceObservation, *, uuid: str, label: str, filesystem: str, min_capacity_bytes: int, required_mount_options: tuple[str, ...] = ()) -> str | None:
    if not observed.present:
        return "USB_MISSING"
    if observed.uuid != uuid or not observed.removable or observed.filesystem != filesystem or (label and observed.label != label):
        return "WRONG_DEVICE"
    if (observed.size or 0) < min_capacity_bytes:
        return "SPACE_LOW"
    if observed.read_only:
        return "USB_READ_ONLY"
    if observed.mounted and required_mount_options:
        options = set(observed.mount_options)
        for required in required_mount_options:
            if required == "umask=077":
                if not ({"fmask=0077", "dmask=0077"} <= options or "umask=0077" in options or "umask=077" in options):
                    return "MOUNT_OPTIONS_UNSAFE"
            elif required in {"uid=0", "gid=0"}:
                continue  # The kernel omits its root-default values from findmnt.
            elif required not in options:
                return "MOUNT_OPTIONS_UNSAFE"
    if observed.dirty:
        return "USB_DIRTY"
    return None


def free_bytes(directory: Path) -> int:
    return shutil.disk_usage(directory).free


def safe_target(directory: Path, name: str) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / name


def fsync_path(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
