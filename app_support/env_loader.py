from __future__ import annotations

import os
from pathlib import Path


def load_project_dotenv(dotenv_path: str | Path | None = None) -> None:
    path = Path(dotenv_path) if dotenv_path else Path(__file__).resolve().parents[1] / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except Exception:
        pass

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
