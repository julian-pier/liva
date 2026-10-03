from __future__ import annotations

import json
import logging
import mimetypes
import re
import sqlite3
import uuid
import base64
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from ai.env import get_openai_api_key
from ai.openai_responses import responses_create
from database.connections import get_core_db, get_nutrition_db
from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:
    HEIF_SUPPORTED = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHYSIQUE_UPLOAD_DIR = PROJECT_ROOT / "uploads" / "physique"
PHYSIQUE_LOCAL_TZ = ZoneInfo("Europe/Berlin")
LOGGER = logging.getLogger(__name__)

VIEW_LABEL_ALIASES = {
    "front": "Front",
    "vorne": "Front",
    "back": "Back",
    "rear": "Back",
    "ruecken": "Back",
    "rücken": "Back",
    "side": "Side",
    "seite": "Side",
}

POSE_LABELS = (
    "Front Relaxed",
    "Front Tense",
    "Front Abs",
    "Front Arm",
    "Side Tense",
    "Back Relaxed",
    "Back Tense",
    "Other",
)

POSE_LABEL_ALIASES = {
    "front relaxed": "Front Relaxed",
    "front neutral": "Front Relaxed",
    "neutral front": "Front Relaxed",
    "front tense": "Front Tense",
    "front chest": "Front Tense",
    "front shoulders": "Front Tense",
    "front abs": "Front Abs",
    "abs": "Front Abs",
    "bauch": "Front Abs",
    "front arm": "Front Arm",
    "arm": "Front Arm",
    "biceps": "Front Arm",
    "bizeps": "Front Arm",
    "side tense": "Side Tense",
    "side chest": "Side Tense",
    "side": "Side Tense",
    "back relaxed": "Back Relaxed",
    "back tense": "Back Tense",
    "lat spread": "Back Tense",
    "back double biceps": "Back Tense",
    "back": "Back Tense",
    "other": "Other",
}

VISION_MODEL = "gpt-4o-mini"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date_iso_from_timestamp(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=PHYSIQUE_LOCAL_TZ)
        return parsed.astimezone(PHYSIQUE_LOCAL_TZ).date().isoformat()
    except Exception:
        return ""


def weight_for_date(date_iso: str | None) -> float | None:
    target = str(date_iso or "").strip()
    if not target:
        return None
    conn = get_nutrition_db()
    try:
        row = conn.execute(
            """
            SELECT weight_kg
            FROM weight_logs
            WHERE date_iso=?
              AND weight_kg IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (target,),
        ).fetchone()
        if not row or row["weight_kg"] is None:
            return None
        return float(row["weight_kg"])
    except Exception:
        return None
    finally:
        conn.close()


def _weight_map_for_dates(date_isos: list[str]) -> dict[str, float]:
    dates = sorted({str(v or "").strip() for v in date_isos if str(v or "").strip()})
    if not dates:
        return {}
    conn = get_nutrition_db()
    try:
        placeholders = ",".join("?" for _ in dates)
        rows = conn.execute(
            f"""
            SELECT date_iso, weight_kg
            FROM weight_logs
            WHERE date_iso IN ({placeholders})
              AND weight_kg IS NOT NULL
            ORDER BY id DESC
            """,
            tuple(dates),
        ).fetchall()
        out: dict[str, float] = {}
        for row in rows:
            key = str(row["date_iso"] or "").strip()
            if not key or key in out or row["weight_kg"] is None:
                continue
            out[key] = float(row["weight_kg"])
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def _parse_exif_datetime(value: Any, offset_value: Any = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    offset_text = str(offset_value or "").strip()
    normalized = text
    try:
        if len(text) >= 19:
            normalized = f"{text[:10].replace(':', '-', 2)}{text[10:]}"
            if offset_text:
                if offset_text == "Z":
                    normalized = normalized + "+00:00"
                elif re.fullmatch(r"[+-]\d{2}:\d{2}", offset_text):
                    normalized = normalized + offset_text
                elif re.fullmatch(r"[+-]\d{4}", offset_text):
                    normalized = normalized + f"{offset_text[:3]}:{offset_text[3:]}"
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=PHYSIQUE_LOCAL_TZ)
        return parsed.replace(microsecond=0).isoformat()
    except Exception:
        return None


def _parse_filename_datetime(name: str | None) -> str | None:
    text = str(name or "").strip()
    if not text:
        return None
    patterns = [
        r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})",
        r"(20\d{2})-(\d{2})-(\d{2})[ _](\d{2})[:.-](\d{2})[:.-](\d{2})",
        r"(20\d{2})[-_](\d{2})[-_](\d{2})(?:[^0-9]|$)",
        r"(20\d{2})(\d{2})(\d{2})(?:[^0-9]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            parts = [int(group) for group in match.groups()]
            parsed = datetime(*parts) if len(parts) == 6 else datetime(*parts[:3])
            if parsed.year > datetime.now(PHYSIQUE_LOCAL_TZ).year + 1:
                continue
            return parsed.replace(tzinfo=PHYSIQUE_LOCAL_TZ, microsecond=0).isoformat()
        except Exception:
            continue
    return None


def _image_metadata_from_bytes(content: bytes, *, filename: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"width": None, "height": None, "captured_at": None, "captured_at_source": None, "exif_tags": {}}
    if not isinstance(content, (bytes, bytearray)) or not content:
        return meta
    try:
        with Image.open(BytesIO(bytes(content))) as image:
            meta["width"], meta["height"] = image.size
            exif = image.getexif() or {}
            meta["exif_tags"] = {
                "DateTimeOriginal": exif.get(36867),
                "DateTimeDigitized": exif.get(36868),
                "DateTime": exif.get(306),
            }
            for tag, offset_tag, source in (
                (36867, 36881, "exif_datetime_original"),
                (36868, 36880, "exif_datetime_digitized"),
                (306, 36880, "exif_datetime"),
            ):
                value = exif.get(tag)
                parsed = _parse_exif_datetime(value, exif.get(offset_tag)) if value else None
                if parsed:
                    meta["captured_at"] = parsed
                    meta["captured_at_source"] = source
                    break
            if not meta["captured_at"]:
                xmp = image.info.get("XML:com.adobe.xmp") or image.info.get("xmp")
                xmp_text = xmp.decode("utf-8", "ignore") if isinstance(xmp, bytes) else str(xmp or "")
                for pattern, source in (
                    (r"(?:xmp:CreateDate|photoshop:DateCreated|CreateDate)\s*=\s*[\"']([^\"']+)", "exif_create_date"),
                    (r"(?:exif:DateTimeDigitized|DateTimeDigitized)\s*=\s*[\"']([^\"']+)", "exif_datetime_digitized"),
                ):
                    match = re.search(pattern, xmp_text, flags=re.IGNORECASE)
                    parsed = _parse_exif_datetime(match.group(1)) if match else None
                    if parsed:
                        meta["captured_at"] = parsed
                        meta["captured_at_source"] = source
                        break
    except (UnidentifiedImageError, OSError, ValueError):
        meta["captured_at"] = None
    except Exception:
        meta["captured_at"] = None
    if not meta["captured_at"]:
        meta["captured_at"] = _parse_filename_datetime(filename)
        if meta["captured_at"]:
            meta["captured_at_source"] = "filename"
    return meta


def _is_heif(filename: str | None, mime_type: str | None) -> bool:
    suffix = Path(str(filename or "")).suffix.lower()
    mime = str(mime_type or "").strip().lower()
    return suffix in {".heic", ".heif", ".heics", ".heifs"} or mime in {"image/heic", "image/heif"}


def _create_preview(content: bytes, *, filename: str, mime_type: str, preview_name: str) -> Path | None:
    try:
        with Image.open(BytesIO(bytes(content))) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.thumbnail((960, 1200), Image.Resampling.LANCZOS)
            preview_path = PHYSIQUE_UPLOAD_DIR / Path(preview_name).name
            image.save(preview_path, format="JPEG", quality=82, optimize=True, progressive=True)
            return preview_path
    except (UnidentifiedImageError, OSError, ValueError):
        LOGGER.warning(
            "physique preview creation skipped for unreadable image",
            extra={"asset_filename": filename, "asset_mime_type": mime_type},
        )
        return None
    except Exception:
        LOGGER.exception(
            "physique preview creation failed",
            extra={"asset_filename": filename, "asset_mime_type": mime_type},
        )
        return None


def _telegram_message_datetime(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=PHYSIQUE_LOCAL_TZ)
        return parsed.astimezone(PHYSIQUE_LOCAL_TZ).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _normalize_view_label(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    for token, label in VIEW_LABEL_ALIASES.items():
        if raw == token.lower() or raw == label.lower():
            return label
    return ""


def _normalize_pose_label(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    for label in POSE_LABELS:
        if raw == label.lower():
            return label
    for token, label in POSE_LABEL_ALIASES.items():
        if raw == token.lower() or raw == label.lower():
            return label
    return ""


def _derive_view_from_pose(pose_label: str | None) -> str:
    pose = str(pose_label or "").strip()
    if pose.startswith("Front"):
        return "Front"
    if pose.startswith("Side"):
        return "Side"
    if pose.startswith("Back"):
        return "Back"
    return ""


def _vision_classify_physique_image(content: bytes, mime_type: str | None = None) -> dict[str, str]:
    api_key = get_openai_api_key()
    if not api_key:
        return {}
    mime = "image/jpeg"
    payload_bytes = bytes(content)
    try:
        with Image.open(BytesIO(payload_bytes)) as image:
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            if max(image.size) > 768:
                image.thumbnail((768, 768))
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=82, optimize=True)
            payload_bytes = buffer.getvalue()
    except Exception:
        payload_bytes = bytes(content)
        mime = str(mime_type or "").strip() or "image/jpeg"
    b64 = base64.b64encode(payload_bytes).decode("ascii")
    prompt = (
        "Klassifiziere dieses Physique-Bild.\n"
        "Erlaube nur diese pose_label-Werte: "
        "Front Relaxed, Front Tense, Front Abs, Front Arm, Side Tense, Back Relaxed, Back Tense, Other.\n"
        'Antworte NUR als JSON: {"pose_label":"..."}'
    )
    try:
        raw = responses_create(
            api_key=api_key,
            model=VISION_MODEL,
            input_messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
                    ],
                }
            ],
            temperature=0.0,
            max_output_tokens=120,
            timeout_s=60,
        ).strip()
        payload = raw
        if not payload.startswith("{"):
            match = re.search(r"\{.*\}", payload, flags=re.DOTALL)
            if match:
                payload = match.group(0)
        data = json.loads(payload)
        if not isinstance(data, dict):
            return {}
        pose_label = _normalize_pose_label(data.get("pose_label")) or ("Other" if str(data.get("pose_label") or "").strip().lower() == "other" else "")
        return {
            "view_label": _derive_view_from_pose(pose_label),
            "pose_label": pose_label,
        }
    except Exception:
        return {}


def ensure_physique_gallery_schema() -> None:
    conn = get_core_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS physique_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'website',
                source_ref TEXT,
                note TEXT NOT NULL DEFAULT '',
                weight_kg REAL,
                view_label TEXT,
                pose_label TEXT,
                captured_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                source_domain TEXT,
                source_chat_id TEXT,
                source_message_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_physique_updates_source_ref
            ON physique_updates(source_ref)
            WHERE source_ref IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS physique_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL,
                asset_name TEXT NOT NULL UNIQUE,
                mime_type TEXT,
                file_size_bytes INTEGER,
                width INTEGER,
                height INTEGER,
                liked INTEGER NOT NULL DEFAULT 0,
                telegram_file_id TEXT,
                created_at TEXT NOT NULL,
                captured_at TEXT,
                captured_at_source TEXT,
                weight_kg REAL,
                weight_source TEXT,
                FOREIGN KEY(update_id) REFERENCES physique_updates(id) ON DELETE CASCADE
            )
            """
        )
        cols = conn.execute("PRAGMA table_info(physique_assets)").fetchall()
        col_names = {str(col[1]) for col in cols}
        if "liked" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN liked INTEGER NOT NULL DEFAULT 0")
        if "original_filename" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN original_filename TEXT")
        if "preview_name" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN preview_name TEXT")
        if "captured_at" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN captured_at TEXT")
        if "captured_at_source" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN captured_at_source TEXT")
        if "weight_kg" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN weight_kg REAL")
        if "weight_source" not in col_names:
            conn.execute("ALTER TABLE physique_assets ADD COLUMN weight_source TEXT")
        conn.commit()
    finally:
        conn.close()
    PHYSIQUE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def ensure_physique_asset_previews() -> dict[str, int]:
    """Create missing browser previews without touching the stored originals."""
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    created = 0
    failed = 0
    try:
        rows = conn.execute(
            """
            SELECT id, asset_name, original_filename, preview_name, mime_type
            FROM physique_assets
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            asset_name = str(row["asset_name"] or "").strip()
            if not asset_name:
                failed += 1
                continue
            current_preview = str(row["preview_name"] or "").strip()
            if current_preview and (PHYSIQUE_UPLOAD_DIR / Path(current_preview).name).is_file():
                continue
            original_path = PHYSIQUE_UPLOAD_DIR / Path(asset_name).name
            if not original_path.is_file():
                failed += 1
                continue
            preview_name = f"{asset_name}.preview.jpg"
            try:
                content = original_path.read_bytes()
            except OSError:
                failed += 1
                continue
            preview_path = _create_preview(
                content,
                filename=str(row["original_filename"] or asset_name),
                mime_type=str(row["mime_type"] or ""),
                preview_name=preview_name,
            )
            if not preview_path:
                failed += 1
                continue
            conn.execute(
                "UPDATE physique_assets SET preview_name=? WHERE id=?",
                (preview_name, int(row["id"])),
            )
            created += 1
        conn.commit()
        return {"created": created, "failed": failed, "total": len(rows)}
    finally:
        conn.close()


def repair_physique_asset_dates(*, dry_run: bool = True) -> dict[str, Any]:
    """Recover per-photo capture dates from stored originals without changing other data."""
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    repaired: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            """
            SELECT id, asset_name, original_filename, captured_at, captured_at_source
            FROM physique_assets
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            asset_name = str(row["asset_name"] or "")
            original_filename = str(row["original_filename"] or asset_name)
            path = PHYSIQUE_UPLOAD_DIR / Path(asset_name).name
            if not path.is_file():
                missing.append({"id": int(row["id"]), "filename": original_filename, "reason": "file_missing"})
                continue
            try:
                meta = _image_metadata_from_bytes(path.read_bytes(), filename=original_filename)
            except OSError:
                missing.append({"id": int(row["id"]), "filename": original_filename, "reason": "read_failed"})
                continue
            found = str(meta.get("captured_at") or "").strip()
            source = str(meta.get("captured_at_source") or "").strip()
            if not found:
                missing.append({"id": int(row["id"]), "filename": original_filename, "reason": "date_missing"})
                continue
            item = {
                "id": int(row["id"]),
                "filename": original_filename,
                "previous": str(row["captured_at"] or ""),
                "captured_at": found,
                "source": source,
            }
            repaired.append(item)
            if not dry_run:
                conn.execute(
                    "UPDATE physique_assets SET captured_at=?, captured_at_source=? WHERE id=?",
                    (found, source, int(row["id"])),
                )
        if not dry_run:
            conn.commit()
        return {"dry_run": bool(dry_run), "repaired": repaired, "missing": missing, "total": len(rows)}
    finally:
        conn.close()


def parse_physique_caption(text: str | None) -> dict[str, Any]:
    raw = str(text or "").strip()
    lower = raw.lower()
    view_label = ""
    pose_label = ""
    for token, label in VIEW_LABEL_ALIASES.items():
        if re.search(rf"(^|[\s,;/\-]){re.escape(token)}($|[\s,;/\-])", lower):
            view_label = label
            break
    for token, label in sorted(POSE_LABEL_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(^|[\s,;/\-]){re.escape(token)}($|[\s,;/\-])", lower):
            pose_label = label
            break
    weight_kg = None
    weight_match = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*kg\b", lower)
    if weight_match:
        try:
            weight_kg = float(weight_match.group(1).replace(",", "."))
        except Exception:
            weight_kg = None
    note = raw
    note = re.sub(r"\bphysique\b", "", note, flags=re.IGNORECASE)
    note = re.sub(r"\b(bodycheck|checkin|check-in)\b", "", note, flags=re.IGNORECASE)
    for token in list(VIEW_LABEL_ALIASES.keys()) + list(POSE_LABEL_ALIASES.keys()):
        note = re.sub(rf"(^|[\s,;/\-]){re.escape(token)}($|[\s,;/\-])", " ", note, flags=re.IGNORECASE)
    note = re.sub(r"\b\d{2,3}(?:[.,]\d)?\s*kg\b", " ", note, flags=re.IGNORECASE)
    note = re.sub(r"\s+", " ", note).strip(" ,;/")
    return {
        "view_label": view_label or _derive_view_from_pose(pose_label),
        "pose_label": pose_label,
        "weight_kg": weight_kg,
        "note": note,
    }


def _extension_for_name(name: str | None, mime_type: str | None = None) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}:
        return suffix
    guessed = mimetypes.guess_extension(str(mime_type or "").strip()) or ""
    if guessed in {".jpe"}:
        guessed = ".jpg"
    return guessed if guessed else ".jpg"


def _load_update_by_source_ref(conn: sqlite3.Connection, source_ref: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM physique_updates WHERE source_ref=? LIMIT 1",
        (str(source_ref or "").strip(),),
    ).fetchone()


def create_physique_update(
    *,
    source: str,
    assets: list[dict[str, Any]],
    note: str = "",
    weight_kg: float | None = None,
    view_label: str = "",
    pose_label: str = "",
    captured_at: str | None = None,
    source_ref: str | None = None,
    source_domain: str | None = None,
    source_chat_id: str | None = None,
    source_message_id: int | None = None,
    telegram_message_date: Any = None,
) -> dict[str, Any]:
    ensure_physique_gallery_schema()
    if not assets:
        raise ValueError("missing_assets")
    explicit_weight = float(weight_kg) if weight_kg is not None else None
    normalized_assets: list[dict[str, Any]] = []
    extracted_candidates: list[tuple[int, str, str]] = []
    for asset in assets:
        content = asset.get("content")
        if not isinstance(content, (bytes, bytearray)) or not content:
            continue
        enriched = dict(asset)
        meta = _image_metadata_from_bytes(bytes(content), filename=asset.get("filename"))
        enriched["_exif_tags"] = meta.get("exif_tags") or {}
        enriched["_captured_at"] = meta.get("captured_at")
        enriched["_captured_at_source"] = meta.get("captured_at_source")
        if enriched.get("width") is None and meta.get("width") is not None:
            enriched["width"] = meta.get("width")
        if enriched.get("height") is None and meta.get("height") is not None:
            enriched["height"] = meta.get("height")
        if meta.get("captured_at"):
            priority = {
                "exif_datetime_original": 1,
                "exif_create_date": 2,
                "exif_datetime_digitized": 2,
                "exif_datetime": 2,
                "filename": 3,
            }.get(str(meta.get("captured_at_source") or ""))
            if priority:
                extracted_candidates.append((priority, str(meta["captured_at"]), str(meta.get("captured_at_source") or "")))
        normalized_assets.append(enriched)
    if not normalized_assets:
        raise ValueError("missing_assets")
    captured = str(captured_at or "").strip()
    selected_date_source = "explicit" if captured else ""
    if not captured and extracted_candidates:
        best_priority = min(item[0] for item in extracted_candidates)
        captured, selected_date_source = min(
            (item[1], item[2]) for item in extracted_candidates if item[0] == best_priority
        )
    if not captured:
        captured = _telegram_message_datetime(telegram_message_date) or ""
        if captured:
            selected_date_source = "telegram_message"
    if not captured:
        captured = _utc_now()
        selected_date_source = "now"
    if weight_kg is None:
        weight_kg = weight_for_date(_date_iso_from_timestamp(captured))
    if not str(view_label or "").strip() and str(pose_label or "").strip():
        view_label = _derive_view_from_pose(pose_label)
    if (not str(view_label or "").strip()) or (not str(pose_label or "").strip()):
        primary = normalized_assets[0]
        detected = _vision_classify_physique_image(
            bytes(primary.get("content") or b""),
            mime_type=str(primary.get("mime_type") or "").strip() or None,
        )
        if not str(view_label or "").strip():
            view_label = str(detected.get("view_label") or "").strip()
            if view_label == "Unknown":
                view_label = ""
        if not str(pose_label or "").strip():
            pose_label = str(detected.get("pose_label") or "").strip()
            if pose_label == "Unknown":
                pose_label = ""
    created_at = _utc_now()
    for asset in normalized_assets:
        asset_captured = str(captured_at or "").strip()
        asset_date_source = "explicit" if asset_captured else ""
        if not asset_captured:
            asset_captured = str(asset.get("_captured_at") or "").strip()
            asset_date_source = str(asset.get("_captured_at_source") or "").strip()
        if not asset_captured:
            asset_captured = _telegram_message_datetime(telegram_message_date) or ""
            if asset_captured:
                asset_date_source = "telegram_message"
        if not asset_captured:
            asset_captured = created_at
            asset_date_source = "now"
        asset["_resolved_captured_at"] = asset_captured
        asset["_resolved_date_source"] = asset_date_source
    asset_weight_map = _weight_map_for_dates([
        _date_iso_from_timestamp(str(asset.get("_resolved_captured_at") or ""))
        for asset in normalized_assets
    ])
    PHYSIQUE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = _load_update_by_source_ref(conn, source_ref) if source_ref else None
        if row:
            update_id = int(row["id"])
            if not note and row["note"]:
                note = str(row["note"] or "")
            if weight_kg is None and row["weight_kg"] is not None:
                weight_kg = float(row["weight_kg"])
            if not view_label and row["view_label"]:
                view_label = str(row["view_label"] or "")
            if not pose_label and row["pose_label"]:
                pose_label = str(row["pose_label"] or "")
        else:
            cur = conn.execute(
                """
                INSERT INTO physique_updates (
                    source, source_ref, note, weight_kg, view_label, pose_label,
                    captured_at, created_at, source_domain, source_chat_id, source_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(source or "website").strip(),
                    str(source_ref or "").strip() or None,
                    str(note or "").strip(),
                    float(weight_kg) if weight_kg is not None else None,
                    str(view_label or "").strip() or None,
                    str(pose_label or "").strip() or None,
                    captured,
                    created_at,
                    str(source_domain or "").strip() or None,
                    str(source_chat_id or "").strip() or None,
                    int(source_message_id) if source_message_id else None,
                ),
            )
            update_id = int(cur.lastrowid)
        for asset in normalized_assets:
            content = asset.get("content")
            if not isinstance(content, (bytes, bytearray)) or not content:
                continue
            ext = _extension_for_name(asset.get("filename"), asset.get("mime_type"))
            asset_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex}{ext}"
            path = PHYSIQUE_UPLOAD_DIR / asset_name
            path.write_bytes(bytes(content))
            preview_name = f"{asset_name}.preview.jpg"
            if not _create_preview(
                bytes(content),
                filename=str(asset.get("filename") or asset_name),
                mime_type=str(asset.get("mime_type") or ""),
                preview_name=preview_name,
            ):
                preview_name = None
            asset_captured = str(asset.get("_resolved_captured_at") or created_at)
            asset_date_source = str(asset.get("_resolved_date_source") or "now")
            asset_date_iso = _date_iso_from_timestamp(asset_captured)
            asset_weight = explicit_weight if explicit_weight is not None else asset_weight_map.get(asset_date_iso)
            asset_weight_source = "explicit" if explicit_weight is not None else ("weight_log_exact_date" if asset_weight is not None else "")
            conn.execute(
                """
                INSERT INTO physique_assets (
                    update_id, asset_name, mime_type, file_size_bytes, width, height,
                    telegram_file_id, original_filename, preview_name, created_at,
                    captured_at, captured_at_source, weight_kg, weight_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    asset_name,
                    str(asset.get("mime_type") or "").strip() or mimetypes.guess_type(path.name)[0],
                    len(content),
                    int(asset.get("width")) if asset.get("width") is not None else None,
                    int(asset.get("height")) if asset.get("height") is not None else None,
                    str(asset.get("telegram_file_id") or "").strip() or None,
                    str(asset.get("filename") or "").strip() or None,
                    preview_name,
                    created_at,
                    asset_captured,
                    asset_date_source,
                    asset_weight,
                    asset_weight_source or None,
                ),
            )
            print(json.dumps({
                "event": "physique_import",
                "telegram_type": str(asset.get("_telegram_media_type") or "image"),
                "original_filename": str(asset.get("filename") or ""),
                "mime_type": str(asset.get("mime_type") or ""),
                "exif_tags": asset.get("_exif_tags") or {},
                "captured_at": asset_captured,
                "date_source": asset_date_source,
                "weight_kg": asset_weight,
                "weight_source": asset_weight_source,
                "original_path": str(path),
                "preview_path": str(PHYSIQUE_UPLOAD_DIR / preview_name) if preview_name else "",
            }, ensure_ascii=False), flush=True)
        conn.commit()
        row = conn.execute("SELECT * FROM physique_updates WHERE id=? LIMIT 1", (update_id,)).fetchone()
        return dict(row) if row else {"id": update_id}
    finally:
        conn.close()


def list_physique_updates(limit: int = 120) -> list[dict[str, Any]]:
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        updates = conn.execute(
            """
            SELECT id, source, note, weight_kg, view_label, pose_label, captured_at, created_at
            FROM physique_updates
            ORDER BY captured_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit or 120), 300)),),
        ).fetchall()
        update_ids = [int(update["id"]) for update in updates]
        assets_by_update: dict[int, list[sqlite3.Row]] = {update_id: [] for update_id in update_ids}
        if update_ids:
            placeholders = ",".join("?" for _ in update_ids)
            asset_rows = conn.execute(
                f"""
                SELECT id, update_id, asset_name, original_filename, preview_name, mime_type, file_size_bytes, width, height, liked, created_at, captured_at, captured_at_source, weight_kg, weight_source
                FROM physique_assets
                WHERE update_id IN ({placeholders})
                ORDER BY update_id ASC, id ASC
                """,
                tuple(update_ids),
            ).fetchall()
            for asset in asset_rows:
                assets_by_update.setdefault(int(asset["update_id"]), []).append(asset)
        weight_dates = [
            _date_iso_from_timestamp(str(asset["captured_at"] or ""))
            for assets in assets_by_update.values()
            for asset in assets
            if asset["weight_kg"] is None
        ]
        weight_map = _weight_map_for_dates(weight_dates)
        rows = []
        for update in updates:
            update_id = int(update["id"])
            assets = assets_by_update.get(update_id, [])
            captured_at = str(update["captured_at"] or "")
            captured_date = _date_iso_from_timestamp(captured_at)
            asset_payloads = []
            for asset in assets:
                asset_captured = str(asset["captured_at"] or captured_at)
                asset_date = _date_iso_from_timestamp(asset_captured)
                stored_asset_weight = asset["weight_kg"]
                if stored_asset_weight is not None:
                    asset_weight = float(stored_asset_weight)
                    asset_weight_source = str(asset["weight_source"] or "")
                elif asset_date in weight_map:
                    asset_weight = weight_map[asset_date]
                    asset_weight_source = "weight_log_exact_date"
                elif len(assets) == 1 and update["weight_kg"] is not None:
                    asset_weight = float(update["weight_kg"])
                    asset_weight_source = "update"
                else:
                    asset_weight = None
                    asset_weight_source = ""
                asset_payloads.append(
                    {
                        "id": int(asset["id"]),
                        "asset_name": str(asset["asset_name"] or ""),
                        "original_filename": str(asset["original_filename"] or ""),
                        "preview_name": str(asset["preview_name"] or ""),
                        "mime_type": str(asset["mime_type"] or ""),
                        "file_size_bytes": int(asset["file_size_bytes"] or 0),
                        "width": int(asset["width"]) if asset["width"] is not None else None,
                        "height": int(asset["height"]) if asset["height"] is not None else None,
                        "liked": bool(int(asset["liked"] or 0)),
                        "created_at": str(asset["created_at"] or ""),
                        "captured_at": asset_captured,
                        "captured_at_source": str(asset["captured_at_source"] or ""),
                        "weight_kg": asset_weight,
                        "weight_source": asset_weight_source,
                    }
                )
            rows.append(
                {
                    "id": update_id,
                    "source": str(update["source"] or ""),
                    "note": str(update["note"] or ""),
                    "weight_kg": (
                        float(update["weight_kg"])
                        if update["weight_kg"] is not None else
                        weight_map.get(captured_date)
                    ),
                    "view_label": str(update["view_label"] or ""),
                    "pose_label": str(update["pose_label"] or ""),
                    "captured_at": captured_at,
                    "created_at": str(update["created_at"] or ""),
                    "assets": asset_payloads,
                }
            )
        return rows
    finally:
        conn.close()


_UNSET = object()


def update_physique_update_tags(
    update_id: int,
    *,
    view_label: str | None | object = _UNSET,
    pose_label: str | None | object = _UNSET,
    weight_kg: float | None | object = _UNSET,
    captured_at: str | None | object = _UNSET,
) -> dict[str, Any] | None:
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, view_label, pose_label, weight_kg, captured_at
            FROM physique_updates
            WHERE id=?
            LIMIT 1
            """,
            (int(update_id),),
        ).fetchone()
        if not row:
            return None
        next_view = row["view_label"] if view_label is _UNSET else (str(view_label or "").strip() or None)
        next_pose = row["pose_label"] if pose_label is _UNSET else (str(pose_label or "").strip() or None)
        next_weight = row["weight_kg"] if weight_kg is _UNSET else (float(weight_kg) if weight_kg is not None else None)
        next_captured = row["captured_at"] if captured_at is _UNSET else (str(captured_at or "").strip() or None)
        conn.execute(
            """
            UPDATE physique_updates
            SET view_label=?, pose_label=?, weight_kg=?, captured_at=?
            WHERE id=?
            """,
            (
                next_view,
                next_pose,
                next_weight,
                next_captured,
                int(update_id),
            ),
        )
        if weight_kg is not _UNSET:
            conn.execute(
                "UPDATE physique_assets SET weight_kg=?, weight_source=? WHERE update_id=?",
                (
                    next_weight,
                    "manual" if next_weight is not None else None,
                    int(update_id),
                ),
            )
        conn.commit()
        updated = conn.execute(
            """
            SELECT id, source, note, weight_kg, view_label, pose_label, captured_at, created_at
            FROM physique_updates
            WHERE id=?
            LIMIT 1
            """,
            (int(update_id),),
        ).fetchone()
        return dict(updated) if updated else {"id": int(update_id)}
    finally:
        conn.close()


def set_physique_asset_liked(asset_id: int, liked: bool) -> dict[str, Any] | None:
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, update_id
            FROM physique_assets
            WHERE id=?
            LIMIT 1
            """,
            (int(asset_id),),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE physique_assets SET liked=? WHERE id=?",
            (1 if liked else 0, int(asset_id)),
        )
        conn.commit()
        return {
            "id": int(row["id"]),
            "update_id": int(row["update_id"]),
            "liked": bool(liked),
        }
    finally:
        conn.close()


def delete_physique_asset(asset_id: int) -> dict[str, Any] | None:
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, update_id, asset_name, preview_name
            FROM physique_assets
            WHERE id=?
            LIMIT 1
            """,
            (int(asset_id),),
        ).fetchone()
        if not row:
            return None
        update_id = int(row["update_id"])
        asset_name = str(row["asset_name"] or "").strip()
        preview_name = str(row["preview_name"] or "").strip() if "preview_name" in row.keys() else ""
        conn.execute("DELETE FROM physique_assets WHERE id=?", (int(asset_id),))
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM physique_assets WHERE update_id=?",
            (update_id,),
        ).fetchone()
        deleted_update = False
        if int((remaining["c"] or 0) if remaining else 0) <= 0:
            conn.execute("DELETE FROM physique_updates WHERE id=?", (update_id,))
            deleted_update = True
        conn.commit()
    finally:
        conn.close()

    if asset_name:
        try:
            path = PHYSIQUE_UPLOAD_DIR / Path(asset_name).name
            if path.exists() and path.is_file():
                path.unlink()
        except Exception:
            pass
    if preview_name:
        try:
            preview_path = PHYSIQUE_UPLOAD_DIR / Path(preview_name).name
            if preview_path.exists() and preview_path.is_file():
                preview_path.unlink()
        except Exception:
            pass
    return {
        "id": int(asset_id),
        "update_id": update_id,
        "deleted_update": bool(deleted_update),
    }


def get_physique_asset_path(asset_name: str) -> Path | None:
    ensure_physique_gallery_schema()
    safe_name = Path(str(asset_name or "")).name
    if not safe_name:
        return None
    path = PHYSIQUE_UPLOAD_DIR / safe_name
    return path if path.exists() and path.is_file() else None


def get_physique_preview_path(preview_name: str) -> Path | None:
    ensure_physique_gallery_schema()
    safe_name = Path(str(preview_name or "")).name
    if not safe_name:
        return None
    path = PHYSIQUE_UPLOAD_DIR / safe_name
    return path if path.exists() and path.is_file() else None


def backfill_physique_labels(limit: int = 500) -> dict[str, int]:
    ensure_physique_gallery_schema()
    conn = get_core_db()
    conn.row_factory = sqlite3.Row
    updated = 0
    scanned = 0
    try:
        rows_raw = conn.execute(
            """
            SELECT id, view_label, pose_label
            FROM physique_updates
            WHERE
                COALESCE(view_label,'')='' OR
                COALESCE(pose_label,'')='' OR
                pose_label IN ('Relaxed', 'Flexed')
            ORDER BY captured_at ASC, id ASC
            LIMIT ?
            """,
            (max(1, min(int(limit or 500), 5000)),),
        ).fetchall()
        rows = [dict(row) for row in rows_raw]
    finally:
        conn.close()
    for row in rows:
        scanned += 1
        conn_asset = get_core_db()
        conn_asset.row_factory = sqlite3.Row
        try:
            asset = conn_asset.execute(
                """
                SELECT asset_name, mime_type
                FROM physique_assets
                WHERE update_id=?
                ORDER BY id ASC
                LIMIT 1
                """,
                (int(row["id"]),),
            ).fetchone()
        finally:
            conn_asset.close()
        if not asset:
            continue
        path = get_physique_asset_path(str(asset["asset_name"] or ""))
        if not path:
            continue
        try:
            content = path.read_bytes()
        except Exception:
            continue
        detected = _vision_classify_physique_image(content, mime_type=str(asset["mime_type"] or "").strip() or None)
        new_view = str(row["view_label"] or "").strip() or str(detected.get("view_label") or "").strip()
        new_pose = str(row["pose_label"] or "").strip() or str(detected.get("pose_label") or "").strip()
        if new_view == "Unknown":
            new_view = ""
        if new_pose == "Unknown":
            new_pose = ""
        if new_view == str(row["view_label"] or "").strip() and new_pose == str(row["pose_label"] or "").strip():
            continue
        saved = False
        for _attempt in range(5):
            conn_write = get_core_db()
            try:
                conn_write.execute(
                    "UPDATE physique_updates SET view_label=?, pose_label=? WHERE id=?",
                    (new_view or None, new_pose or None, int(row["id"])),
                )
                conn_write.commit()
                saved = True
                break
            except sqlite3.OperationalError:
                time.sleep(0.25)
            finally:
                conn_write.close()
        if not saved:
            continue
        updated += 1
    return {"scanned": scanned, "updated": updated}
