"""Align source_missing with durable S3 keys / legacy ciphertext.

Usage:
    python -m app.fact_memory.backfill_sources
    python -m app.fact_memory.backfill_sources 1fb0c9b7aea4462bb51e4b48606e794a

Docs with source_blob_key (SOURCE_FILE_RETENTION=s3) count as present.
There is no NYSCEF re-fetch for legacy missing rows.
"""

from __future__ import annotations

import sys

from app.fact_memory.store import get_store


def main(argv: list[str]) -> int:
    store = get_store()
    flagged = store.flag_missing_sources()
    print(f"updated {flagged} document(s) source_missing flags")
    for job_id in argv:
        status = store.source_status_for_job(job_id)
        print(f"{job_id}: {status.get('status')} document={status.get('document_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
