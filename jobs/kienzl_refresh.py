from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from kienzl import kienzl
import sqlite3
from database.connections import get_training_db


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fallback_phrases(action_id: str, n: int) -> list[str]:
    base = (kienzl.SEED_PHRASES.get(action_id) or []).copy()
    if not base:
        return []
    out = []
    i = 0
    while len(out) < n and i < 200:
        i += 1
        s = base[i % len(base)].strip()
        if not s:
            continue
        # tiny mutations to avoid exact duplicates
        if i % 3 == 0 and not s.endswith("Punkt."):
            s = (s.rstrip(".") + ". Punkt.").strip()
        if i % 5 == 0:
            s = s.replace("fertig.", "Punkt.").replace("Punkt.", "fertig.")
        out.append(s)
    # de-dupe
    uniq = []
    seen = set()
    for t in out:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq[:n]


def main() -> int:
    kienzl.ensure_kienzl_schema_and_seed()

    last_ts = kienzl.get_state("refresh_last_ts")
    if not last_ts:
        last_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")

    action_ids = kienzl.refresh_needed_actions(last_ts)
    if not action_ids:
        kienzl.set_state("refresh_last_ts", _utc_iso())
        print("KIENZL_REFRESH: no used actions")
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    use_openai = bool(api_key)
    generator = None
    if use_openai:
        try:
            from ai.kienzl_client import generate_kienzl_phrases
            generator = generate_kienzl_phrases
        except Exception as e:
            print("KIENZL_REFRESH: openai client unavailable:", e)
            use_openai = False

    refreshed = 0
    for aid in action_ids:
        cur = kienzl.count_active_phrases(aid)
        missing = max(0, kienzl.POOL_SIZE - cur)
        if missing <= 0:
            continue

        if use_openai and generator:
            try:
                # pull prompt_seed from DB via cached daily helper (simple)
                conn = get_training_db()
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute("SELECT prompt_seed FROM kienzl_actions WHERE id=?", (aid,)).fetchone()
                    prompt_seed = (row["prompt_seed"] if row else "") or ""
                finally:
                    conn.close()
                avoid = kienzl.last_texts_for_action(aid, n=5)
                phrases = generator(prompt_seed=prompt_seed, avoid=avoid, n=missing)
                n_ins = kienzl.insert_phrases(aid, phrases)
                refreshed += 1 if n_ins else 0
                print(f"KIENZL_REFRESH: {aid} +{n_ins} (openai)")
                continue
            except Exception as e:
                print(f"KIENZL_REFRESH: {aid} openai failed:", e)

        # fallback: local variants
        phrases = _fallback_phrases(aid, missing)
        n_ins = kienzl.insert_phrases(aid, phrases)
        refreshed += 1 if n_ins else 0
        print(f"KIENZL_REFRESH: {aid} +{n_ins} (fallback)")

    kienzl.set_state("refresh_last_ts", _utc_iso())
    print(f"KIENZL_REFRESH: done (actions={len(action_ids)}, refreshed={refreshed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
