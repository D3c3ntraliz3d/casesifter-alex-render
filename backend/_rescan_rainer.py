"""Queue a paced rescan of Rainer 521800/2022 through the running API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
CASE = "860770c6696d405198a4c2606deaf525"
FILING = {
    "complaint",
    "motion",
    "affidavit",
    "affirmation",
    "answer",
    "order",
    "notice",
    "letter",
    "brief",
    "memorandum",
    "reply",
    "opposition",
    "filing",
}


def _request(url: str, method: str = "GET") -> dict:
    last_error: Exception | None = None
    for attempt in range(8):
        try:
            request = urllib.request.Request(
                url,
                method=method,
                data=b"" if method == "POST" else None,
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(3 + attempt * 2)
    raise RuntimeError(f"{method} {url} failed: {last_error}") from last_error


def get(url: str) -> dict:
    return _request(url, "GET")


def post(url: str) -> dict:
    return _request(url, "POST")


def rank(doc: dict) -> int:
    kind = (doc.get("doc_kind") or "").lower()
    if kind in FILING:
        return 0
    if kind in {"", "unknown"}:
        return 1
    return 2


def busy_docs(case: dict) -> list[dict]:
    return [doc for doc in case.get("documents") or [] if doc.get("status") in {"queued", "scanning"}]


def main() -> None:
    case = get(f"{BASE}/api/cases/{CASE}")
    docs = sorted(case.get("documents") or [], key=rank)
    print(f"Rescanning {len(docs)} documents in {case.get('caption')} {case.get('index_number')}", flush=True)
    started = 0
    start_failures = 0
    for doc in docs:
        waited = 0
        while True:
            busy = busy_docs(get(f"{BASE}/api/cases/{CASE}"))
            if len(busy) < 2:
                break
            time.sleep(8)
            waited += 8
            if waited >= 48:
                print("BUSY STALE — continuing so a new scan can start", flush=True)
                break
        name = doc.get("display_name") or doc.get("original_filename")
        try:
            post(f"{BASE}/api/cases/{CASE}/documents/{doc['id']}/scan")
            started += 1
            print(f"QUEUED {started}/{len(docs)} {name}", flush=True)
        except urllib.error.HTTPError as exc:
            start_failures += 1
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            print(f"START FAIL {name} HTTP {exc.code} {detail}", flush=True)
        except Exception as exc:
            start_failures += 1
            print(f"START FAIL {name} {exc}", flush=True)
        time.sleep(1)

    while True:
        case = get(f"{BASE}/api/cases/{CASE}")
        busy = busy_docs(case)
        complete = sum(1 for doc in case["documents"] if doc.get("status") == "complete")
        failed = sum(1 for doc in case["documents"] if doc.get("status") == "failed")
        print(
            f"PROGRESS complete={complete} failed={failed} busy={len(busy)} started={started} start_failures={start_failures}",
            flush=True,
        )
        if not busy:
            break
        time.sleep(20)
    print("RESCAN COMPLETE", flush=True)


if __name__ == "__main__":
    main()
