"""Apply filename-first kinds on the Lefkowitz case. Does not rescan."""

from collections import Counter

from app.auto_title import retitle_case_documents
from app.cases_db import list_documents

CASE_ID = "71c0b0b4dd314c73b4c5cbaec025bdd3"

if __name__ == "__main__":
    changes = retitle_case_documents(CASE_ID)
    kinds = Counter((doc.get("doc_kind") or "untyped") for doc in list_documents(CASE_ID))
    print(f"updated {len(changes)} documents")
    for item in changes:
        before = item.get("kind_before") or ""
        after = item.get("kind_after") or ""
        if before != after:
            print(f"  {item.get('after') or item.get('before')}: {before or 'untyped'} -> {after}")
    print("kind counts", dict(kinds))
