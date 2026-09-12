"""Anonymized finding telemetry for continuous evals — never sends exhibit binaries."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app import cases_db
from app.config import settings
from app.models import Finding


def share_enabled() -> bool:
    flag = cases_db.get_setting(
        "share_anonymized_outcomes",
        "true" if settings.share_anonymized_outcomes else "false",
    )
    return flag == "true"


def emit_scan_outcomes(
    *,
    doc_kind: str | None,
    findings: list[Finding],
    risk_level: str,
) -> str | None:
    if not share_enabled():
        return None
    payload = {
        "doc_kind": doc_kind or "unknown",
        "risk_level": risk_level,
        "findings": [
            {
                "category": f.category,
                "severity": f.severity,
                "confidence": round(float(f.confidence), 1),
                "analysis_type": (f.evidence or {}).get("analysis_type"),
                "case_consistency": bool((f.evidence or {}).get("case_consistency")),
                "field": (f.evidence or {}).get("field"),
            }
            for f in findings
            if f.severity != "info"
        ],
    }
    return cases_db.record_telemetry("scan_outcomes", payload)


def record_finding_feedback(
    *,
    finding_id: str,
    useful: bool,
    category: str | None = None,
    field: str | None = None,
    doc_kind: str | None = None,
) -> str:
    payload = {
        "finding_id_hash": finding_id[:24],
        "useful": useful,
        "category": category,
        "field": field,
        "doc_kind": doc_kind,
    }
    event_id = cases_db.record_telemetry("finding_feedback", payload)
    _try_flush()
    return event_id


def flush_to_cloud(limit: int = 50) -> dict[str, Any]:
    """Push unsynced anonymized events to the control plane when configured."""
    events = cases_db.list_unsynced_telemetry(limit=limit)
    if not events:
        return {"flushed": 0}
    if not settings.cloud_control_url:
        # Local-only: mark as synced so the queue does not grow forever in open mode.
        cases_db.mark_telemetry_synced([e["id"] for e in events])
        return {"flushed": len(events), "destination": "local"}

    token = cases_db.get_setting("cloud_session_token") or ""
    body = json.dumps(
        {
            "events": [
                {
                    "id": e["id"],
                    "type": e["event_type"],
                    "payload": e["payload"],
                    "created_at": e["created_at"],
                }
                for e in events
            ]
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        settings.cloud_control_url.rstrip("/") + "/v1/telemetry",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            _ = resp.read()
        cases_db.mark_telemetry_synced([e["id"] for e in events])
        return {"flushed": len(events), "destination": "cloud"}
    except urllib.error.URLError as exc:
        return {"flushed": 0, "error": str(exc.reason)}


def _try_flush() -> None:
    try:
        flush_to_cloud(limit=20)
    except Exception:
        pass
