"""Promote anonymized finding_feedback events into eval fixture stubs.

Usage (from backend/):
  python -m evals.promote_feedback
  python -m evals.promote_feedback --practice insurance
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from app import cases_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote thumbs into fixture stubs")
    parser.add_argument("--practice", default="foreclosure")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    cases_db.ensure_schema()
    rows = cases_db.list_unsynced_telemetry(limit=args.limit)
    # Also accept already-synced feedback from a full scan of recent events if needed.
    feedback = [r for r in rows if r.get("event_type") == "finding_feedback"]
    out_dir = Path(__file__).resolve().parent / "fixtures" / args.practice / "from_feedback"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamped = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written = 0
    for row in feedback:
        payload = row.get("payload") or {}
        useful = bool(payload.get("useful"))
        stub = {
            "id": f"feedback_{row['id'][:12]}",
            "practice": args.practice,
            "source_event": row["id"],
            "created_at": stamped,
            "label": "must_ship" if useful else "must_suppress",
            "must_ship": useful,
            "must_suppress": not useful,
            "anonymized": {
                "category": payload.get("category"),
                "field": payload.get("field"),
                "doc_kind": payload.get("doc_kind"),
                "finding_id_hash": payload.get("finding_id_hash"),
            },
            "notes": "Stub only — attach quotes manually before promoting to a live eval case.",
        }
        dest = out_dir / f"{stub['id']}.json"
        dest.write_text(json.dumps(stub, indent=2), encoding="utf-8")
        written += 1
    print(f"wrote {written} stub(s) under {out_dir}")


if __name__ == "__main__":
    main()
