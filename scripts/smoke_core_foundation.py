from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _print(label: str, value: str) -> None:
    print(f"[core-foundation] {label}: {value}")


def main() -> int:
    os.environ.setdefault("LIVA_FLASK_SECRET", "core-foundation-smoke-secret")
    try:
        from app import app
        from core.core_daily_board import ensure_core_daily_boards_schema
        from core.core_training_card import ensure_core_training_card_schema
        from core.core_memory_layers import (
            ensure_core_memory_consolidations_schema,
            ensure_core_memory_events_schema,
            ensure_core_memory_review_log_schema,
        )
        from core.core_memory_file_patches import ensure_core_memory_file_patches_schema
    except Exception as exc:
        _print("import_error", str(exc))
        return 1

    modules = [
        "core.core_daily_board",
        "core.core_training_card",
        "core.core_memory_layers",
        "core.core_memory_file_patches",
        "ai.actions_v2",
    ]
    for name in modules:
        importlib.import_module(name)
        _print("import_ok", name)

    ensure_calls = [
        ("core_daily_boards", ensure_core_daily_boards_schema),
        ("core_training_cards", ensure_core_training_card_schema),
        ("core_memory_events", ensure_core_memory_events_schema),
        ("core_memory_consolidations", ensure_core_memory_consolidations_schema),
        ("core_memory_review_log", ensure_core_memory_review_log_schema),
        ("core_memory_file_patches", ensure_core_memory_file_patches_schema),
    ]
    for label, func in ensure_calls:
        func()
        func()
        _print("ensure_ok", f"{label} x2")

    with app.test_client() as client:
        checks = [
            ("GET /api/v2/actions/core/board", "/api/v2/actions/core/board?date=2026-04-27"),
            ("GET /api/v2/actions/core/training-card", "/api/v2/actions/core/training-card?date=2026-04-27"),
            ("GET /api/v2/actions/core/memory/layers", "/api/v2/actions/core/memory/layers?date=2026-04-27"),
            ("GET /api/v2/actions/core/memory/file-patches", "/api/v2/actions/core/memory/file-patches"),
            ("GET /api/core/memory/inbox", "/api/core/memory/inbox"),
            ("GET /api/core/memory/consolidations", "/api/core/memory/consolidations"),
            ("GET /api/core/memory/file-patches", "/api/core/memory/file-patches"),
        ]
        for label, url in checks:
            response = client.get(url)
            _print(label, str(response.status_code))

    _print("done", "foundation smoke completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
