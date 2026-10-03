from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .memory_policy import MemoryPolicy
from .memos_write import MemosWriteClient


ALLOWED_VISIBLE_TAGS = {"#liva", "#gpt", "#privat", "#training", "#recovery", "#ernaehrung", "#schule", "#system", "#kreativ"}
TECHNICAL_TAGS = {
    "#dauerhaft", "#scope_permanent", "#scope_week", "#scope_day", "#scope_temporary",
    "#scope_until_changed", "#diese_woche", "#aktuell", "#kurzfristig", "#temporär", "#temporary",
}
_TECHNICAL_PREFIXES = ("#diese_woche_",)
_DOMAIN_TAGS = {
    "ernaehrung": {"#ernaehrung", "#nutrition", "#makros", "#calories", "#mealprep", "#essen", "#mahlzeit"},
    "training": {"#training", "#gym", "#exercise", "#progression", "#cardio", "#bootcamp"},
    "recovery": {"#recovery", "#hrv", "#sleep", "#fatigue", "#schlaf"},
    "schule": {"#school", "#schule", "#klausur", "#abi"},
    "system": {"#system", "#mcp", "#codex", "#server", "#software", "#test", "#e2e", "#regression"},
    "kreativ": {"#kreativ", "#poetry", "#text", "#schreiben", "#gedicht"},
    "privat": {"#beziehung", "#person", "#glaube", "#emotion", "#privat"},
}
_TYPE_TAGS = {
    "ereignis": {"#ereignis"},
    "person": {"#person"},
    "emotion": {"#emotion", "#gefühl"},
    "erkenntnis": {"#erkenntnis", "#beobachtung"},
    "praeferenz": {"#praeferenz", "#vorliebe"},
    "entscheidung": {"#entscheidung"},
    "status": {"#status", "#next_session", "#planung", "#progression"},
    "technik": {"#technik", "#mcp", "#codex", "#server", "#software", "#test", "#e2e", "#regression"},
}


def _load_env(path: Path) -> None:
    for raw in path.read_text().splitlines():
        if "=" not in raw or raw.strip().startswith("#"):
            continue
        key, value = raw.strip().split("=", 1)
        os.environ[key] = value.strip().strip("'\"")


def _client(env_file: str) -> MemosWriteClient:
    if env_file:
        _load_env(Path(env_file))
    return MemosWriteClient(
        os.environ.get("LIVA_MCP_MEMOS_BASE_URL"),
        os.environ.get("LIVA_MCP_MEMOS_READ_TOKEN") or os.environ.get("LIVA_MCP_MEMOS_WRITE_TOKEN"),
        os.environ.get("LIVA_MCP_MEMOS_WRITE_TOKEN"),
    )


def _all(client: MemosWriteClient) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for state in ("NORMAL", "ARCHIVED"):
        page = None
        for _ in range(10):
            query = {"pageSize": 100, "state": state}
            if page:
                query["pageToken"] = page
            payload = client._request("GET", "?" + urlencode(query))
            result.extend(row for row in payload.get("memos", []) if isinstance(row, dict))
            page = payload.get("nextPageToken") or payload.get("next_page_token")
            if not page:
                break
    return result


def _tags(content: str) -> list[str]:
    """Only classify a leading system-tag line; body hashtags are user content."""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        result = []
        for part in parts:
            if not part.startswith("#"):
                break
            result.append(part.casefold())
        trailing = []
        for tail in reversed(lines):
            values = tail.strip().split()
            if not values:
                continue
            if all(value.startswith("#") for value in values):
                trailing.extend(value.casefold() for value in values)
            break
        return result + trailing
    return []


def _is_technical(tag: str) -> bool:
    return tag in TECHNICAL_TAGS or tag.startswith(_TECHNICAL_PREFIXES)


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _metadata_path() -> Path:
    return Path(os.environ.get("LIVA_MCP_WRITE_STATE") or (Path(os.environ.get("LIVA_MCP_AUTH_DB", Path.cwd())).resolve().parent / "liva_mcp_writes.sqlite3"))


def _metadata(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            return {str(row["memo_name"]): dict(row) for row in conn.execute("SELECT * FROM memory_metadata")}
    except sqlite3.Error:
        return {}


def _metadata_count(path: Path) -> int:
    return sum(1 for row in _metadata(path).values() if not row.get("legacy_metadata"))


def report(client: MemosWriteClient, policy: MemoryPolicy, metadata_path: Path) -> dict[str, Any]:
    rows = _all(client)
    tag_counts: Counter[str] = Counter()
    no_origin = duplicate_tags = technical = over_two = no_domain = 0
    flow_hashtags: Counter[str] = Counter()
    bodies: Counter[str] = Counter()
    over_three = 0
    for row in rows:
        tags = _tags(str(row.get("content") or ""))
        tag_counts.update(tags)
        if not any(tag in {"#gpt", "#liva"} for tag in tags):
            no_origin += 1
        if len(tags) != len(set(tags)):
            duplicate_tags += 1
        if any(tag not in ALLOWED_VISIBLE_TAGS for tag in tags):
            technical += 1
        if len(tags) > 2:
            over_two += 1
        if any(tag in {"#gpt", "#liva"} for tag in tags) and not any(tag in {"#privat", "#training", "#recovery", "#ernaehrung", "#schule", "#system", "#kreativ"} for tag in tags): no_domain += 1
        for line in str(row.get("content") or "").splitlines()[1:]:
            flow_hashtags.update(part.casefold() for part in line.split() if part.startswith("#"))
        bodies[" ".join(str(row.get("content") or "").casefold().split())] += 1
    return {
        "policy_version": policy.version,
        "memo_count": len(rows),
        "gpt_count": tag_counts["#gpt"],
        "liva_count": tag_counts["#liva"],
        "without_origin": no_origin,
        "duplicate_tag_entries": duplicate_tags,
        "direct_user_memos": no_origin,
        "non_allowed_visible_tag_entries": technical,
        "over_two_visible_system_tags": over_two,
        "memos_without_domain": no_domain,
        "unresolved_domains": sum(1 for row in _metadata(metadata_path).values() if row.get("origin") and not row.get("domain")),
        "unresolved_memory_types": sum(1 for row in _metadata(metadata_path).values() if row.get("origin") and not row.get("memory_type")),
        "technical_scope_time_tag_entries": technical,
        "over_three_visible_tags": over_two,
        "possible_body_hashtags": flow_hashtags.most_common(20),
        "most_common_tags": tag_counts.most_common(20),
        "possible_duplicate_groups": sum(1 for count in bodies.values() if count > 1),
        "structured_metadata_count": _metadata_count(metadata_path),
    }


def _decision(value: str | None, source: str, confidence: str, reason: str) -> dict[str, Any]:
    return {"value": value, "source": source, "confidence": confidence, "reason": reason}


def _one_from_tags(tags: set[str], candidates: dict[str, set[str]], kind: str) -> dict[str, Any]:
    matches = [name for name, values in candidates.items() if values & tags]
    if len(matches) == 1:
        return _decision(matches[0], "legacy_tags", "high", f"eindeutige {kind}-Tag-Zuordnung")
    if not matches:
        return _decision(None, "none", "unresolved", f"keine eindeutige {kind}-Zuordnung")
    return _decision(None, "legacy_tags", "unresolved", f"mehrdeutige {kind}-Tags: {', '.join(sorted(matches))}")


def _classify(row: dict[str, Any], policy: MemoryPolicy, metadata: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tags = set(_tags(str(row.get("content") or "")))
    if "#gpt" in tags:
        origin = _decision("liva", "v2_snapshot_gpt_cutover", "high", "verbindliche Altbestandsregel: Snapshot-#gpt ist LIVA")
    elif metadata and metadata.get("origin") in policy.origins:
        origin = _decision(str(metadata["origin"]), "structured_metadata", "high", "vorhandene strukturierte Herkunft")
    elif "#liva" in tags:
        origin = _decision("liva", "visible_liva_tag", "high", "explizites altes LIVA-Tag")
    else:
        origin = _decision(None, "none", "high", "keine belastbare Autorenquelle; ohne Präfix beibehalten")
    if metadata and metadata.get("domain") in policy.domains:
        domain = _decision(str(metadata["domain"]), "structured_metadata", "high", "vorhandene strukturierte Domain")
    else:
        domain = _one_from_tags(tags, _DOMAIN_TAGS, "Domain")
    if metadata and metadata.get("memory_type") in policy.memory_types:
        memory_type = _decision(str(metadata["memory_type"]), "structured_metadata", "high", "vorhandener strukturierter Memory-Type")
    else:
        memory_type = _one_from_tags(tags, _TYPE_TAGS, "Memory-Type")
    return origin, domain, memory_type


def _body_without_tags(content: str) -> str:
    lines = content.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        leading = 0
        for part in parts:
            if not part.startswith("#"):
                break
            leading += 1
        if leading:
            remainder = " ".join(parts[leading:])
            rest = lines[index + 1:]
            body = ([remainder] if remainder else []) + rest
            while body and not body[-1].strip():
                body.pop()
            if body and all(value.startswith("#") for value in body[-1].strip().split()):
                body.pop()
            return "\n".join(body).lstrip("\n").rstrip()
        return content.strip()
    return ""


def normalization_plan(client: MemosWriteClient, policy: MemoryPolicy, metadata_path: Path) -> dict[str, Any]:
    metadata_rows = _metadata(metadata_path)
    actions = []
    for row in _all(client):
        name = str(row.get("name") or "")
        content = str(row.get("content") or "")
        old_tags = _tags(content)
        origin, domain, memory_type = _classify(row, policy, metadata_rows.get(name))
        can_normalize = origin["value"] is not None
        new_tags = policy.tags(origin["value"], domain["value"] if domain["confidence"] == "high" else None) if can_normalize else []
        new_content = (" ".join(new_tags) + "\n\n" if new_tags else "") + _body_without_tags(content) if can_normalize else content
        changed = new_content != content
        if not changed and can_normalize:
            continue
        reasons = [origin["reason"], domain["reason"], memory_type["reason"]]
        actions.append({
            "memo_name": name,
            "old_content_sha256": _sha(content),
            "new_content_sha256": _sha(new_content),
            "old_visible_tags": old_tags,
            "new_visible_tags": new_tags if can_normalize else old_tags,
            "origin_decision": origin,
            "domain_decision": domain,
            "memory_type_decision": memory_type,
            "confidence": "high" if can_normalize else "user_unchanged",
            "reason": "; ".join(reasons),
            "created_at": row.get("createTime"),
            "updated_at": row.get("updateTime"),
            "state": row.get("state"),
            "change_type": "normalize_v2_visible_tags" if can_normalize else "user_content_unchanged",
        })
    return {"policy_version": policy.version, "mode": "plan_only", "write_applied": False, "generated_at": datetime.now(timezone.utc).isoformat(), "actions": actions}


def _backup(client: MemosWriteClient, plan: dict[str, Any], path: Path, metadata_path: Path) -> Path:
    rows = {str(row.get("name")): row for row in _all(client)}
    metadata_rows = _metadata(metadata_path)
    relations: list[dict[str, Any]] = []
    if metadata_path.exists():
        with sqlite3.connect(metadata_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                relations = [dict(row) for row in conn.execute("SELECT * FROM memory_relations")]
            except sqlite3.Error:
                pass
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "memos": list(rows.values()),
        "metadata": metadata_rows,
        "relations": relations,
        "affected_memo_names": [action["memo_name"] for action in plan.get("actions", []) if action.get("confidence") == "high"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def _record_legacy_metadata(metadata_path: Path, action: dict[str, Any]) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(metadata_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS memory_metadata (memo_name TEXT PRIMARY KEY, origin TEXT, domain TEXT, memory_type TEXT, temporal_scope TEXT, event_date TEXT, valid_from TEXT, valid_until TEXT, status TEXT, supersedes TEXT, superseded_by TEXT, edited_at TEXT, policy_version INTEGER, legacy_metadata INTEGER NOT NULL DEFAULT 0)")
        existing = conn.execute("SELECT memo_name FROM memory_metadata WHERE memo_name=?", (action["memo_name"],)).fetchone()
        if not existing:
            conn.execute("INSERT INTO memory_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)", (action["memo_name"], action["origin_decision"]["value"], action["domain_decision"]["value"], action["memory_type_decision"]["value"], "historical", None, action.get("created_at"), None, str(action.get("state") or "NORMAL").lower(), None, None, None, 2))
        else:
            conn.execute("UPDATE memory_metadata SET origin=?, domain=COALESCE(?, domain), memory_type=COALESCE(?, memory_type), policy_version=2 WHERE memo_name=?", (action["origin_decision"]["value"], action["domain_decision"]["value"], action["memory_type_decision"]["value"], action["memo_name"]))
        conn.commit()


def apply_plan(client: MemosWriteClient, plan: dict[str, Any], metadata_path: Path, backup_file: Path) -> dict[str, Any]:
    backup = _backup(client, plan, backup_file, metadata_path)
    applied = []
    review = []
    for action in plan.get("actions", []):
        if action.get("confidence") != "high":
            review.append({"memo_name": action.get("memo_name"), "reason": action.get("reason"), "old_visible_tags": action.get("old_visible_tags"), "proposed_visible_tags": action.get("new_visible_tags")})
            continue
        current = client.get(str(action["memo_name"]))
        content = str(current.get("content") or "")
        if _sha(content) != action.get("old_content_sha256"):
            review.append({"memo_name": action.get("memo_name"), "reason": "content_hash_changed_since_plan", "old_visible_tags": action.get("old_visible_tags"), "proposed_visible_tags": action.get("new_visible_tags")})
            continue
        old_created, old_updated = current.get("createTime"), current.get("updateTime")
        new_content = (" ".join(action["new_visible_tags"]) + "\n\n" if action["new_visible_tags"] else "") + _body_without_tags(content)
        updated = client.update_content(str(action["memo_name"]), new_content)
        after = client.get(str(action["memo_name"]))
        if after.get("createTime") != old_created or after.get("updateTime") != old_updated:
            client.update_content(str(action["memo_name"]), content)
            raise RuntimeError("timestamp_changed_during_migration; restored current memo and stopped")
        if _sha(str(after.get("content") or "")) != action.get("new_content_sha256"):
            client.update_content(str(action["memo_name"]), content)
            raise RuntimeError("content_verification_failed; restored current memo and stopped")
        _record_legacy_metadata(metadata_path, action)
        applied.append(str(action["memo_name"]))
    review_file = backup.with_name(backup.stem + ".review.json")
    review_file.write_text(json.dumps({"review": review}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    review_file.chmod(0o600)
    return {"applied": len(applied), "review": len(review), "backup": str(backup), "backup_sha256": _sha(backup.read_text(encoding="utf-8")), "review_file": str(review_file), "memo_names": applied}


def rollback(client: MemosWriteClient, backup_file: Path, metadata_path: Path) -> dict[str, Any]:
    payload = json.loads(backup_file.read_text(encoding="utf-8"))
    restored = []
    for memo in payload.get("memos", []):
        name, content = str(memo["name"]), str(memo.get("content") or "")
        current = client.get(name)
        if str(current.get("content") or "") != content:
            client.update_content(name, content)
            after = client.get(name)
            if after.get("createTime") != memo.get("createTime") or after.get("updateTime") != memo.get("updateTime"):
                raise RuntimeError("rollback_timestamp_verification_failed")
            restored.append(name)
    original_metadata = payload.get("metadata") or {}
    affected = [str(name) for name in payload.get("affected_memo_names") or []]
    if affected:
        with sqlite3.connect(metadata_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memory_metadata (memo_name TEXT PRIMARY KEY, origin TEXT, domain TEXT, memory_type TEXT, temporal_scope TEXT, event_date TEXT, valid_from TEXT, valid_until TEXT, status TEXT, supersedes TEXT, superseded_by TEXT, edited_at TEXT, policy_version INTEGER, legacy_metadata INTEGER NOT NULL DEFAULT 0)")
            conn.executemany("DELETE FROM memory_metadata WHERE memo_name=?", [(name,) for name in affected])
            for name in affected:
                row = original_metadata.get(name)
                if row:
                    conn.execute("INSERT INTO memory_metadata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row.get(key) for key in ("memo_name", "origin", "domain", "memory_type", "temporal_scope", "event_date", "valid_from", "valid_until", "status", "supersedes", "superseded_by", "edited_at", "policy_version", "legacy_metadata")))
            conn.commit()
    return {"restored": len(restored), "backup": str(backup_file), "metadata_restored": len(affected)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit, migrate, and rollback safe LIVA Memory Policy V1 normalization")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--metadata-path", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report")
    sub.add_parser("normalization-plan")
    apply = sub.add_parser("apply-plan")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--backup-file", required=True)
    apply.add_argument("--confirm", action="store_true")
    restore = sub.add_parser("rollback")
    restore.add_argument("--backup-file", required=True)
    restore.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    client = _client(args.env_file)
    repo = Path(os.environ.get("LIVA_MCP_REPO_ROOT", Path.cwd()))
    policy = MemoryPolicy.load(repo / "config" / "liva_memory_policy.yaml")
    metadata_path = Path(args.metadata_path) if args.metadata_path else _metadata_path()
    if args.command == "report":
        print(json.dumps(report(client, policy, metadata_path), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "normalization-plan":
        print(json.dumps(normalization_plan(client, policy, metadata_path), ensure_ascii=False, sort_keys=True))
        return 0
    if not args.confirm:
        raise SystemExit(f"{args.command} requires --confirm")
    if args.command == "apply-plan":
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        print(json.dumps(apply_plan(client, plan, metadata_path, Path(args.backup_file)), ensure_ascii=False, sort_keys=True))
        return 0
    print(json.dumps(rollback(client, Path(args.backup_file), metadata_path), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
