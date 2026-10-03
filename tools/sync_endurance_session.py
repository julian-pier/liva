from __future__ import annotations

import argparse
import json

from database.connections import get_plans_db
from integrations.endurance_intervals_sync import (
    _enqueue,
    build_intervals_payload,
    ensure_sync_schema,
    load_session,
    payload_hash,
    run_sync_jobs,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Eine LIVA-Endurance-Session für Intervals prüfen oder synchronisieren.")
    parser.add_argument("session_id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    conn = get_plans_db(); ensure_sync_schema(conn)
    session = load_session(conn, args.session_id)
    if not session:
        parser.error("Session nicht gefunden")
    payload = build_intervals_payload(session)
    preview = {"name": payload["name"], "date": payload["start_date_local"][:10], "external_id": payload["external_id"], "description": payload["description"], "payload_hash": payload_hash(payload)}
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if args.execute:
        _enqueue(conn, args.session_id, "UPSERT", payload["external_id"]); conn.commit(); conn.close()
        print(json.dumps(run_sync_jobs(session_id=args.session_id), ensure_ascii=False))
    else:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
