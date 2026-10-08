"""Flag reports whose original file was deleted with the ephemeral job.

Usage:
    python -m app.fact_memory.backfill_sources
    python -m app.fact_memory.backfill_sources 1fb0c9b7aea4462bb51e4b48606e794a

There is no NYSCEF re-fetch. A job is either still holding bytes or source_missing.
"""

from __future__ import annotations

import sys

from app.fact_memory.store import get_store


def main(argv: list[str]) -> int:
    store = get_store()
    flagged = store.flag_missing_sources()
    print(f"flagged {flagged} document(s) source_missing")
    for job_id in argv:
        status = store.source_status_for_job(job_id)
        print(f"{job_id}: {status.get('status')} document={status.get('document_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
