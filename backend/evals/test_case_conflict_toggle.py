"""CASE_FACT_CONFLICTS switch: hides case-wide fact-conflict cards, keeps forensics."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


def _finding(fid: str, severity: str, title: str, *, analysis_type: str | None = None, category: str = "contextual"):
    ev = {"analysis_type": analysis_type} if analysis_type else {}
    if analysis_type == "fact_conflict":
        ev.update({"documentary": True, "field": "mortgage_origination", "case_impact": True})
    return {
        "id": fid,
        "category": category,
        "severity": severity,
        "title": title,
        "explanation": title,
        "why_suspicious": title,
        "confidence": 90,
        "evidence": ev,
    }


FORENSIC = _finding("f1", "medium", "Statement due date precedes its own period", analysis_type="date_inconsistency")
CONFLICT_A = _finding("c1", "high", "Mortgage date does not match recording date", analysis_type="fact_conflict", category="case_consistency")
CONFLICT_B = _finding("c2", "high", "Principal appears as more than one figure", analysis_type="fact_conflict", category="case_consistency")
STANDING = _finding("s1", "high", "Assignment executed after commencement", analysis_type="standing_timeline", category="case_consistency")
CRFN = _finding("r1", "high", "Mortgage CRFN may already be discharged of record", category="case_consistency")
GOTCHA = _finding("g1", "high", "Utility bill issued before the provider served the area", analysis_type="historical_impossibility", category="gotcha")
ALL = [FORENSIC, GOTCHA, CONFLICT_A, CONFLICT_B, STANDING, CRFN]
KEPT_IDS = ["f1", "g1"]


def _report() -> dict:
    findings = list(ALL)
    return {
        "job_id": "a" * 32,
        "risk_level": "red",
        "risk_score": 100,
        "executive_summary": "high risk",
        "top_findings": [CONFLICT_A, CONFLICT_B, FORENSIC],
        "recommended_actions": [],
        "findings": findings,
        "audit": {"filename": "Exhibit 41.pdf"},
    }


def _settings(monkeypatch, enabled: bool):
    monkeypatch.setenv("CASE_FACT_CONFLICTS", "true" if enabled else "false")
    from app.config import Settings
    import app.config as config_mod

    s = Settings()
    monkeypatch.setattr(config_mod, "settings", s)
    return s


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("CASE_FACT_CONFLICTS", raising=False)
    from app.config import Settings

    assert Settings().case_fact_conflicts is False


def test_strip_report_hides_conflicts_and_rescores(monkeypatch):
    _settings(monkeypatch, enabled=False)
    from app.case_conflict_toggle import strip_report

    original = _report()
    out = strip_report(original)
    assert [f["id"] for f in out["findings"]] == KEPT_IDS
    assert all(f["id"] in KEPT_IDS for f in out["top_findings"])
    assert out["risk_score"] < 100
    assert out["case_fact_conflicts_hidden"] == 4
    # Stored report untouched (setting is read-time only).
    assert len(original["findings"]) == 6 and original["risk_score"] == 100


def test_strip_report_noop_when_on(monkeypatch):
    _settings(monkeypatch, enabled=True)
    from app.case_conflict_toggle import strip_report

    original = _report()
    assert strip_report(original) is original


@pytest.mark.parametrize("enabled", [False, True])
def test_matter_endpoints_follow_setting(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-key-toggle")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "test-master-key-toggle")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = _settings(monkeypatch, enabled)
    monkeypatch.setattr("app.main.settings", settings, raising=False)
    monkeypatch.setattr("app.v1_analysis.settings", settings, raising=False)

    from app.fact_memory.store import get_store, reset_store

    reset_store()
    import app.main as main_mod

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    headers = {"X-API-Key": "test-key-toggle"}

    matter = client.post("/api/v1/matters", headers=headers, json={"name": "toggle"}).json()
    mid = matter["id"]
    firm_id = matter["firm_id"]
    result = get_store().persist_scan(
        firm_id=firm_id,
        matter_id=mid,
        job_id="b" * 32,
        sha256="c" * 64,
        original_filename="Exhibit 41.pdf",
        display_name="Exhibit 41",
        doc_type="exhibit",
        filed_date=None,
        page_count=3,
        entities={},
        facts=[],
        findings=list(ALL),
        text_fingerprint=None,
        text_excerpt="",
        page_offsets=None,
        report=_report(),
    )
    doc_id = result["document_id"]

    board = client.get(f"/api/v1/matters/{mid}/findings", headers=headers).json()
    types = {
        (f.get("evidence") or {}).get("analysis_type") or f.get("finding_type")
        for f in board["findings"]
    }
    report = client.get(f"/api/v1/matters/{mid}/documents/{doc_id}/report", headers=headers).json()
    detail = client.get(f"/api/v1/matters/{mid}", headers=headers).json()
    doc_row = next(d for d in detail["documents"] if d["id"] == doc_id)

    if enabled:
        assert "fact_conflict" in types
        assert len(report["findings"]) == 6
        assert report["risk_score"] == 100
        assert doc_row["risk_level"] == "red"
    else:
        assert types == {"date_inconsistency", "historical_impossibility"}, types
        assert board["fact_conflicts"] == []
        assert all(
            (g.get("analysis_type") not in {"fact_conflict", "standing_timeline"})
            for g in board["gotchas"]
        )
        assert [f["id"] for f in report["findings"]] == KEPT_IDS
        assert report["risk_score"] < 100
    reset_store()
