from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json


@dataclass(frozen=True)
class BackupRecord:
    bundle: Path
    receipt: Path
    backup_id: str
    finished_at: datetime
    status: str
    restore_tested: bool = False


def discover(directory: Path) -> list[BackupRecord]:
    records: list[BackupRecord] = []
    for receipt in directory.glob("LIVA-FR-v1_*.tar.zst.age.receipt.json"):
        try:
            raw = json.loads(receipt.read_text(encoding="utf-8"))
            if raw.get("format_version") != "LIVA-FR-v1" or raw.get("status") not in {"transport_verified", "restore_tested"}:
                continue
            bundle = receipt.with_name(receipt.name.removesuffix(".receipt.json"))
            if not bundle.is_file():
                continue
            finished = datetime.fromisoformat(str(raw["finished_at"]).replace("Z", "+00:00"))
            records.append(BackupRecord(bundle, receipt, str(raw["backup_id"]), finished, str(raw["status"]), bool(raw.get("restore_tested"))))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return sorted(records, key=lambda item: item.finished_at, reverse=True)


def protected(records: list[BackupRecord]) -> set[Path]:
    keep = {item.bundle for item in records[:21]}
    months: set[tuple[int, int]] = set()
    for item in records:
        marker = (item.finished_at.year, item.finished_at.month)
        if marker not in months and len(months) < 12:
            months.add(marker); keep.add(item.bundle)
    if records:
        keep.add(records[0].bundle)
    tested = next((item for item in records if item.restore_tested), None)
    if tested:
        keep.add(tested.bundle)
    return keep


def prune(directory: Path) -> list[Path]:
    records = discover(directory)
    keep = protected(records)
    removed: list[Path] = []
    for item in records:
        if item.bundle in keep:
            continue
        for path in (item.bundle, item.bundle.with_name(item.bundle.name + ".sha256"), item.receipt):
            if path.exists():
                path.unlink(); removed.append(path)
    return removed
