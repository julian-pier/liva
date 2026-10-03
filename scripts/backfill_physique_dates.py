"""Repair Telegram Physique dates from the stored image bytes.

Dry-run is the default. Use ``--apply`` only after reviewing the report.
Only ``captured_at`` is changed; all other Physique fields remain untouched.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database.connections import get_core_db  # noqa: E402
from training.physique_gallery import (  # noqa: E402
    PHYSIQUE_UPLOAD_DIR,
    _date_iso_from_timestamp,
    _image_metadata_from_bytes,
    ensure_physique_gallery_schema,
)


def _candidate_for_update(assets: list[dict]) -> tuple[str, str] | None:
    candidates: list[tuple[int, str, str]] = []
    priorities = {
        "exif_datetime_original": 1,
        "exif_create_date": 2,
        "exif_datetime_digitized": 2,
        "exif_datetime": 2,
        "filename": 3,
    }
    for asset in assets:
        path = PHYSIQUE_UPLOAD_DIR / Path(str(asset.get("asset_name") or "")).name
        if not path.is_file():
            continue
        try:
            metadata = _image_metadata_from_bytes(
                path.read_bytes(),
                # asset_name is generated at import time and must never be
                # treated as the user's original filename.
                filename=str(asset.get("original_filename") or ""),
            )
        except OSError:
            continue
        source = str(metadata.get("captured_at_source") or "")
        timestamp = str(metadata.get("captured_at") or "")
        if timestamp and source in priorities:
            candidates.append((priorities[source], timestamp, source))
    if not candidates:
        return None
    priority = min(item[0] for item in candidates)
    timestamp, source = min((item[1], item[2]) for item in candidates if item[0] == priority)
    return timestamp, source


def run(*, apply: bool = False) -> int:
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        if apply:
            ensure_physique_gallery_schema()
        asset_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(physique_assets)").fetchall()}
        original_filename_sql = "original_filename" if "original_filename" in asset_columns else "NULL AS original_filename"
        updates = conn.execute(
            "SELECT id, captured_at FROM physique_updates WHERE source='telegram' ORDER BY id"
        ).fetchall()
        print("Bild/ID | bisheriges Datum | gefundenes Ursprungsdatum | Quelle")
        print("--- | --- | --- | ---")
        misses: list[str] = []
        changed = 0
        for update in updates:
            assets = conn.execute(
                f"SELECT id, asset_name, {original_filename_sql} FROM physique_assets WHERE update_id=? ORDER BY id",
                (int(update["id"]),),
            ).fetchall()
            candidate = _candidate_for_update([dict(asset) for asset in assets])
            image_id = ", ".join(f"{int(asset['id'])}:{asset['asset_name']}" for asset in assets) or f"update:{int(update['id'])}"
            old = str(update["captured_at"] or "")
            if not candidate:
                misses.append(image_id)
                print(f"{image_id} | {old} | — | —")
                continue
            timestamp, source = candidate
            print(f"{image_id} | {old} | {timestamp} | {source}")
            if apply and _date_iso_from_timestamp(old) != _date_iso_from_timestamp(timestamp):
                conn.execute("UPDATE physique_updates SET captured_at=? WHERE id=?", (timestamp, int(update["id"])))
                changed += 1
        if apply:
            conn.commit()
        print(f"\nTreffer: {len(updates) - len(misses)} · Ohne Treffer: {len(misses)} · Geändert: {changed}")
        if misses:
            print("\nOhne rekonstruierbares Ursprungsdatum:")
            for image_id in misses:
                print(f"- {image_id}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Änderungen schreiben; ohne Flag nur Dry-Run")
    args = parser.parse_args()
    raise SystemExit(run(apply=args.apply))
