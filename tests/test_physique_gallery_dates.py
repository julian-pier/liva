from datetime import date
from io import BytesIO
from pathlib import Path
import sqlite3

from PIL import Image
from PIL import ImageOps
import pillow_heif

import database.connections as connections
import integrations.telegram_hub as telegram_hub
from training import physique_gallery


def _jpeg_with_exif(*, original=None, digitized=None):
    image = Image.new("RGB", (2, 2), "white")
    exif = image.getexif()
    if original:
        exif[36867] = original
    if digitized:
        exif[36868] = digitized
    stream = BytesIO()
    image.save(stream, format="JPEG", exif=exif.tobytes())
    return stream.getvalue()


def _heic_with_exif(*, original=None, orientation=None):
    image = Image.new("RGB", (20, 10), "red")
    exif = image.getexif()
    if original:
        exif[36867] = original
    if orientation:
        exif[274] = orientation
    image.info["exif"] = exif.tobytes()
    stream = BytesIO()
    pillow_heif.from_pillow(image).save(stream, format="HEIF")
    return stream.getvalue()


def test_physique_metadata_prefers_datetime_original_over_digitized():
    metadata = physique_gallery._image_metadata_from_bytes(
        _jpeg_with_exif(original="2024:01:02 03:04:05", digitized="2025:06:07 08:09:10"),
        filename="IMG_2026-07-23_120000.jpg",
    )
    assert metadata["captured_at_source"] == "exif_datetime_original"
    assert physique_gallery._date_iso_from_timestamp(metadata["captured_at"]) == "2024-01-02"


def test_physique_filename_and_telegram_date_fallbacks():
    metadata = physique_gallery._image_metadata_from_bytes(b"not-an-image", filename="IMG_20240131_221530.jpg")
    assert metadata["captured_at_source"] == "filename"
    assert physique_gallery._date_iso_from_timestamp(metadata["captured_at"]) == "2024-01-31"
    message_time = physique_gallery._telegram_message_datetime("2024-02-01T23:30:00Z")
    assert physique_gallery._date_iso_from_timestamp(message_time) == "2024-02-02"


def test_create_physique_update_uses_telegram_date_after_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    row = physique_gallery.create_physique_update(
        source="telegram",
        assets=[{"content": b"not-an-image", "filename": "telegram-photo.jpg", "mime_type": "image/jpeg"}],
        view_label="Front",
        pose_label="Front Relaxed",
        source_ref="test:1",
        telegram_message_date="2024-03-04T23:30:00Z",
    )
    assert physique_gallery._date_iso_from_timestamp(row["captured_at"]) == "2024-03-05"
    assert date.fromisoformat(physique_gallery._date_iso_from_timestamp(row["captured_at"])) > date(2024, 1, 1)


def test_jpeg_import_creates_small_browser_preview_without_changing_original(monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    image = Image.new("RGB", (1800, 2400), "blue")
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=92)
    original = stream.getvalue()

    physique_gallery.create_physique_update(
        source="upload",
        assets=[{"content": original, "filename": "camera.jpg", "mime_type": "image/jpeg"}],
        captured_at="2024-01-02T03:04:05+01:00",
    )

    asset = physique_gallery.list_physique_updates(limit=1)[0]["assets"][0]
    assert asset["preview_name"].endswith(".preview.jpg")
    original_path = physique_gallery.get_physique_asset_path(asset["asset_name"])
    preview_path = physique_gallery.get_physique_preview_path(asset["preview_name"])
    assert original_path is not None and original_path.read_bytes() == original
    assert preview_path is not None
    with Image.open(preview_path) as preview:
        assert preview.width <= 960
        assert preview.height <= 1200


def test_multi_image_upload_keeps_distinct_capture_date_per_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")

    physique_gallery.create_physique_update(
        source="website",
        assets=[
            {"content": _jpeg_with_exif(original="2024:01:02 03:04:05"), "filename": "first.jpg", "mime_type": "image/jpeg"},
            {"content": _jpeg_with_exif(original="2025:06:07 08:09:10"), "filename": "second.jpg", "mime_type": "image/jpeg"},
        ],
    )

    assets = physique_gallery.list_physique_updates(limit=1)[0]["assets"]
    assert len(assets) == 2
    assert physique_gallery._date_iso_from_timestamp(assets[0]["captured_at"]) == "2024-01-02"
    assert assets[0]["captured_at_source"] == "exif_datetime_original"
    assert physique_gallery._date_iso_from_timestamp(assets[1]["captured_at"]) == "2025-06-07"
    assert assets[1]["captured_at_source"] == "exif_datetime_original"


def test_multi_image_upload_assigns_weight_per_asset_capture_date(monkeypatch, tmp_path):
    core_db = tmp_path / "core.sqlite3"
    nutrition_db = tmp_path / "nutrition.sqlite3"
    monkeypatch.setattr(connections, "CORE_DB", str(core_db))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(nutrition_db))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    conn = sqlite3.connect(nutrition_db)
    conn.execute("CREATE TABLE weight_logs (id INTEGER PRIMARY KEY, date_iso TEXT, weight_kg REAL)")
    conn.executemany(
        "INSERT INTO weight_logs (date_iso, weight_kg) VALUES (?, ?)",
        [("2024-01-02", 81.2), ("2025-06-07", 73.4)],
    )
    conn.commit()
    conn.close()

    physique_gallery.create_physique_update(
        source="website",
        assets=[
            {"content": _jpeg_with_exif(original="2024:01:02 03:04:05"), "filename": "first.jpg", "mime_type": "image/jpeg"},
            {"content": _jpeg_with_exif(original="2025:06:07 08:09:10"), "filename": "second.jpg", "mime_type": "image/jpeg"},
            {"content": _jpeg_with_exif(original="2023:03:04 05:06:07"), "filename": "without-weight.jpg", "mime_type": "image/jpeg"},
        ],
    )

    listed = physique_gallery.list_physique_updates(limit=1)[0]
    by_date = {
        physique_gallery._date_iso_from_timestamp(asset["captured_at"]): asset
        for asset in listed["assets"]
    }
    assert by_date["2024-01-02"]["weight_kg"] == 81.2
    assert by_date["2024-01-02"]["weight_source"] == "weight_log_exact_date"
    assert by_date["2025-06-07"]["weight_kg"] == 73.4
    assert by_date["2023-03-04"]["weight_kg"] is None

    # A legacy group-level value must never leak onto every image.
    conn = sqlite3.connect(core_db)
    conn.execute("UPDATE physique_updates SET weight_kg=99.9")
    conn.execute("UPDATE physique_assets SET weight_kg=NULL, weight_source=NULL")
    conn.commit()
    conn.close()
    legacy_assets = physique_gallery.list_physique_updates(limit=1)[0]["assets"]
    legacy_by_date = {
        physique_gallery._date_iso_from_timestamp(asset["captured_at"]): asset
        for asset in legacy_assets
    }
    assert legacy_by_date["2024-01-02"]["weight_kg"] == 81.2
    assert legacy_by_date["2025-06-07"]["weight_kg"] == 73.4
    assert legacy_by_date["2023-03-04"]["weight_kg"] is None


def test_gallery_only_assigns_thumbnail_urls_near_the_viewport():
    script = (Path(__file__).parents[1] / "static" / "js" / "physique_gallery.js").read_text(encoding="utf-8")

    assert 'img data-src="' in script
    assert "new IntersectionObserver" in script
    assert 'rootMargin: "320px 0px"' in script


def test_gallery_renders_each_asset_as_its_own_photo_and_has_mobile_compare_gestures():
    root = Path(__file__).parents[1]
    script = (root / "static" / "js" / "physique_gallery.js").read_text(encoding="utf-8")
    stylesheet = (root / "static" / "css" / "physique_gallery.css").read_text(encoding="utf-8")

    assert 'state.assets.map(function (asset, globalIndex)' in script
    assert '<article class="physique-photo"' in script
    assert 'viewerGrid.addEventListener("touchstart"' in script
    assert 'viewerGrid.addEventListener("touchend"' in script
    assert "splitFromSingle();" in script
    assert "exitSplit();" in script
    assert "compareAssetIds" in script
    assert "openSelectedComparison" in script
    assert "if (state.compareAssetIds.length === 2) openSelectedComparison();" in script
    assert 'data-selection-open="1"' in (root / "templates" / "physique_gallery.html").read_text(encoding="utf-8")
    assert "state.zoom[paneIndex]" in script
    assert 'pane.addEventListener("touchmove"' in script
    assert "Math.min(5, gesture.scale * ratio)" in script
    assert 'document.addEventListener("pointerdown"' not in script
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in stylesheet
    assert ".physique-viewer[hidden]:not(.is-open)" in stylesheet
    assert "pointer-events: none !important;" in stylesheet
    assert "grid-template-rows: repeat(2, minmax(0, 1fr)) !important;" in stylesheet
    assert "Raster stays image-only on touch devices" in stylesheet
    assert ".physique-photo-actions {\n    display: none !important;" in stylesheet
    assert 'data-viewer-like="1"' in script
    assert 'data-viewer-delete="1"' in script
    assert "viewerActionGroup.hidden = hasSplit" in script
    assert 'actionIcon("like", asset.liked)' in script
    assert 'actionIcon("delete")' in script
    assert "🗑" not in script
    assert "♥" not in script
    assert "♡" not in script


def test_normal_telegram_photo_uses_largest_photo_and_keeps_exif_date(monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    original = _jpeg_with_exif(original="2023:11:12 14:15:16")
    thumbnail = _jpeg_with_exif(original=None)
    monkeypatch.setattr(telegram_hub, "resolve_domain_config", lambda domain: {"token": "test", "chat_id": "1"})
    monkeypatch.setattr(
        telegram_hub,
        "_api_get_file_bytes",
        lambda token, file_id: (original, "image/jpeg", "camera.jpg") if file_id == "large" else (thumbnail, "image/jpeg", "thumb.jpg"),
    )
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda *args, **kwargs: {"ok": True})

    result = telegram_hub.process_training_media_message(
        {
            "message_id": 901,
            "date": 1700000000,
            "caption": "Physique front relaxed",
            "chat": {"id": 1},
            # Deliberately unsorted: the EXIF-bearing large photo is first.
            "photo": [
                {"file_id": "large", "width": 2000, "height": 3000},
                {"file_id": "thumb", "width": 100, "height": 150},
            ],
        }
    )

    assert result["ok"] is True
    conn = connections.get_core_db()
    row = conn.execute("SELECT captured_at FROM physique_updates WHERE source_ref='tg-training:1:901'").fetchone()
    conn.close()
    assert row is not None
    assert physique_gallery._date_iso_from_timestamp(row[0]) == "2023-11-12"


def test_normal_telegram_photo_without_exif_uses_message_time(monkeypatch, tmp_path):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    photo = _jpeg_with_exif()
    monkeypatch.setattr(telegram_hub, "resolve_domain_config", lambda domain: {"token": "test", "chat_id": "1"})
    monkeypatch.setattr(telegram_hub, "_api_get_file_bytes", lambda token, file_id: (photo, "image/jpeg", "photo.jpg"))
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda *args, **kwargs: {"ok": True})
    result = telegram_hub.process_training_media_message({
        "message_id": 903,
        "date": 1700000000,
        "chat": {"id": 1},
        "photo": [{"file_id": "photo", "width": 1000, "height": 1000}],
    })
    assert result["ok"] is True
    row = connections.get_core_db().execute("SELECT captured_at FROM physique_updates WHERE source_ref='tg-training:1:903'").fetchone()
    assert physique_gallery._date_iso_from_timestamp(row[0]) == "2023-11-14"


def test_heic_document_keeps_original_and_creates_oriented_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    original = _heic_with_exif(original="2021:05:06 07:08:09", orientation=6)
    row = physique_gallery.create_physique_update(
        source="telegram",
        assets=[{
            "content": original,
            "filename": "bodycheck.heic",
            "mime_type": "image/heic",
            "_telegram_media_type": "document",
        }],
        view_label="Front",
        pose_label="Front Relaxed",
        source_ref="test:heic",
    )
    assert physique_gallery._date_iso_from_timestamp(row["captured_at"]) == "2021-05-06"
    listed = physique_gallery.list_physique_updates(limit=1)[0]
    asset = listed["assets"][0]
    assert asset["original_filename"] == "bodycheck.heic"
    assert asset["preview_name"].endswith(".preview.jpg")
    original_path = physique_gallery.get_physique_asset_path(asset["asset_name"])
    preview_path = physique_gallery.get_physique_asset_path(asset["preview_name"])
    assert original_path is not None and original_path.read_bytes() == original
    assert preview_path is not None
    with Image.open(preview_path) as preview:
        # pillow-heif normalizes the synthetic fixture's orientation while
        # decoding; the preview still has the correct preserved aspect ratio.
        assert preview.size == (20, 10)


def test_jpeg_document_exif_and_filename_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    first = physique_gallery.create_physique_update(
        source="telegram",
        assets=[{"content": _jpeg_with_exif(original="2020:01:02 03:04:05"), "filename": "x.jpg", "mime_type": "image/jpeg"}],
        view_label="Front", pose_label="Front Relaxed", source_ref="test:jpeg",
    )
    second = physique_gallery.create_physique_update(
        source="telegram",
        assets=[{"content": b"not-an-image", "filename": "IMG_20191231_235959.jpg", "mime_type": "image/jpeg"}],
        view_label="Front", pose_label="Front Relaxed", source_ref="test:name",
    )
    assert physique_gallery._date_iso_from_timestamp(first["captured_at"]) == "2020-01-02"
    assert physique_gallery._date_iso_from_timestamp(second["captured_at"]) == "2019-12-31"


def test_telegram_heic_document_preserves_filename_mime_and_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "CORE_DB", str(tmp_path / "core.sqlite3"))
    monkeypatch.setattr(connections, "NUTRITION_DB", str(tmp_path / "nutrition.sqlite3"))
    monkeypatch.setattr(physique_gallery, "PHYSIQUE_UPLOAD_DIR", tmp_path / "uploads")
    heic = _heic_with_exif(original="2022:02:03 04:05:06")
    monkeypatch.setattr(telegram_hub, "resolve_domain_config", lambda domain: {"token": "test", "chat_id": "1"})
    monkeypatch.setattr(telegram_hub, "_api_get_file_bytes", lambda token, file_id: (heic, "application/octet-stream", "telegram-generated.heic"))
    monkeypatch.setattr(telegram_hub, "send_domain_message", lambda *args, **kwargs: {"ok": True})
    result = telegram_hub.process_training_media_message({
        "message_id": 902,
        "date": 1700000000,
        "chat": {"id": 1},
        "document": {"file_id": "doc", "file_name": "original-bodycheck.heic", "mime_type": "image/heic"},
    })
    assert result["ok"] is True
    row = physique_gallery.list_physique_updates(limit=1)[0]
    asset = row["assets"][0]
    assert physique_gallery._date_iso_from_timestamp(row["captured_at"]) == "2022-02-03"
    assert asset["original_filename"] == "original-bodycheck.heic"
    assert asset["mime_type"] == "image/heic"
    assert asset["preview_name"]
