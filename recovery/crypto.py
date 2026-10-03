from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


class CryptoError(RuntimeError):
    pass


def age_binary() -> str | None:
    return shutil.which("age")


def recipient_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if not lines:
        return None
    if not all(line.startswith("age1") for line in lines):
        raise CryptoError("recipients file contains no native age recipient")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

