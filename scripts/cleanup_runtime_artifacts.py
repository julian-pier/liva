from __future__ import annotations

import argparse
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
RUNTIME_SUFFIXES = {"-wal", "-shm", "-journal"}


def _is_cleanup_candidate(path: Path) -> bool:
    name = path.name
    if any(name.endswith(suffix) for suffix in RUNTIME_SUFFIXES):
        return True
    return any(name.endswith(suffix) for suffix in DB_SUFFIXES)


def find_empty_runtime_artifacts(root: Path) -> list[Path]:
    matches: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not _is_cleanup_candidate(path):
            continue
        try:
            if path.stat().st_size == 0:
                matches.append(path)
        except FileNotFoundError:
            continue
    return sorted(matches)


def cleanup_empty_runtime_artifacts(root: Path, *, dry_run: bool = False) -> list[Path]:
    matches = find_empty_runtime_artifacts(root)
    if dry_run:
        return matches
    removed: list[Path] = []
    for path in matches:
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove zero-byte local DB/runtime artifacts from the repo tree.")
    parser.add_argument("--root", default=str(REPO_ROOT), help="Repo root to scan")
    parser.add_argument("--dry-run", action="store_true", help="Only list files, do not delete them")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    removed = cleanup_empty_runtime_artifacts(root, dry_run=args.dry_run)
    action = "Would remove" if args.dry_run else "Removed"
    for path in removed:
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        print(f"{action}: {rel}")
    print(f"{action} {len(removed)} zero-byte runtime artifact(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
