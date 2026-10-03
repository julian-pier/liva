#!/usr/bin/env python3
"""Fail when tracked source is unsuitable for a public LIVA release."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

RUNTIME_PREFIXES = (".backup/", "backups/", "backup-staging/", "data/", "logs/", "output/", "uploads/", "var/", "strava_sync/data/", "smart_home/data/")
SENSITIVE_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx", ".age")
SENSITIVE_NAMES = (".env", "credentials", "id_rsa", "private_key")
EMAIL_PATTERN = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
HOME_PATTERN = re.compile(r"/(?:home|Users)/[^/\s]+")
LEGACY_PATTERN = re.compile(
    rf"(?i)\b(?:{'training' + 'hub'}|{'juli' + 'an'})\b|{'private' + 'relay'}"
)
SECRET_PATTERN = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AIza[0-9A-Za-z_-]{30,}"
)
TAILNET_HOST_PATTERN = re.compile(
    r"(?i)\b[a-z0-9-]+\." + "ts" + r"\.net\b"
)


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    return [root / name.decode() for name in result.stdout.split(b"\0") if name]


def path_problem(relative: Path) -> str | None:
    value = relative.as_posix()
    name = relative.name.lower()
    if value.startswith(RUNTIME_PREFIXES):
        return "tracked runtime or personal-data path"
    if name.endswith(SENSITIVE_SUFFIXES) or any(marker in name for marker in (".sqlite.", ".sqlite3.", ".db.")):
        return "credential or database-shaped file"
    if name == ".env" or any(item in name for item in SENSITIVE_NAMES):
        if not name.endswith((".example", ".sample", ".template")):
            return "credential-shaped filename"
    return None


def content_problems(content: str) -> list[str]:
    problems: list[str] = []
    if HOME_PATTERN.search(content):
        problems.append("absolute user-home path")
    if LEGACY_PATTERN.search(content):
        problems.append("personal or legacy identifier")
    if SECRET_PATTERN.search(content):
        problems.append("credential-like value")
    if TAILNET_HOST_PATTERN.search(content):
        problems.append("private tailnet hostname")
    for email in EMAIL_PATTERN.findall(content):
        domain = email.rsplit("@", 1)[1].lower()
        if not (
            domain in {"example.com", "example.org", "example.net"}
            or domain.endswith((".example", ".invalid", ".test", ".localhost"))
        ):
            problems.append("non-example e-mail address")
            break
    return problems


def audit(root: Path) -> list[str]:
    findings: list[str] = []
    for file_path in tracked_files(root):
        relative = file_path.relative_to(root)
        if problem := path_problem(relative):
            findings.append(f"{relative}: {problem}")
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(f"{relative}: {item}" for item in content_problems(content))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        findings = audit(root)
    except subprocess.CalledProcessError:
        print("ERROR: run this audit inside a Git working tree.", file=sys.stderr)
        return 2
    if not findings:
        print("PASS: tracked source meets the public-release gate.")
        return 0
    print("BLOCKED: public release contains unsafe tracked content:")
    print("\n".join(f"- {finding}" for finding in findings))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
