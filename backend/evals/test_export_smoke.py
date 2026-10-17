"""Smoke: v1 export endpoints return 200 + correct content-type from durable report."""

from __future__ import annotations

import importlib
import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("APP_MODE", "analysis")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault(
    "FACT_MEMORY_MASTER_KEY",
    "dGVzdC1tYXN0ZXIta2V5LTEyMzQ1Njc4OTBhYmNkZWY=",
)
os.environ.setdefault("ANALYSIS_API_KEYS", "export-smoke-key")

from fastapi.testclient import TestClient


def _fixture_report(job_id: str) -> dict:
    from app.models import AuditTrail, Report

    report = Report(
        job_id=job_id,
        risk_level="green",
        risk_score=0,
        ai_review="complete",
        executive_summary="Smoke export fixture — no material findings.",
        recommended_actions=["No further action required from this smoke fixture."],
        findings=[],
        top_findings=[],
        timeline=[],
        page_dimensions=[],
        fonts=[],
        comparison={},
        modules=[],
        audit=AuditTrail(
            scanned_at=datetime.now(timezone.utc),
            analysis_version="test",
            modules_run=["smoke"],
            submitted_by="export-smoke",
            filename="smoke-exhibit.pdf",
            file_size_bytes=100,
            file_type="application/pdf",
            sha256="b" * 64,
        ),
        artifact_urls=[],
    )
    return report.model_dump(mode="json")


def test_v1_export_html_pdf_json(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "export-smoke-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    monkeypatch.setenv(
        "FACT_MEMORY_MASTER_KEY",
        "dGVzdC1tYXN0ZXIta2V5LTEyMzQ1Njc4OTBhYmNkZWY=",
    )

    from app.config import Settings
    import app.config as config_mod
    import app.main as main_mod
    import app.v1_analysis as v1_mod

    settings = Settings(app_mode="analysis", analysis_api_keys="export-smoke-key")
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr(main_mod, "settings", settings)
    monkeypatch.setattr(v1_mod, "settings", settings)

    job_id = uuid.uuid4().hex
    fixture = _fixture_report(job_id)

    monkeypatch.setattr(
        v1_mod,
        "require_job_owner",
        lambda job_id, auth: {
            "job": {
                "job_id": job_id,
                "status": "complete",
                "document_id": "doc-smoke",
                "matter_id": "matter-smoke",
                "firm_id": "firm-smoke",
            },
            "firm_ctx": {"firm_id": "firm-smoke", "user_id": "user-smoke"},
        },
    )
    monkeypatch.setattr(v1_mod, "_resolve_report_dict", lambda job_id, owned: fixture)
    monkeypatch.setattr(v1_mod, "load_progress", lambda job_id: None)

    importlib.reload(main_mod)
    # Re-apply after reload (reload rebinds settings from env).
    monkeypatch.setattr(main_mod, "settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr(
        "app.v1_analysis.require_job_owner",
        lambda job_id, auth: {
            "job": {
                "job_id": job_id,
                "status": "complete",
                "document_id": "doc-smoke",
                "matter_id": "matter-smoke",
                "firm_id": "firm-smoke",
            },
            "firm_ctx": {"firm_id": "firm-smoke", "user_id": "user-smoke"},
        },
    )
    monkeypatch.setattr(
        "app.v1_analysis._resolve_report_dict",
        lambda job_id, owned: fixture,
    )

    client = TestClient(main_mod.app)
    headers = {"X-API-Key": "export-smoke-key"}

    html = client.get(f"/api/v1/jobs/{job_id}/export/html", headers=headers)
    assert html.status_code == 200, html.text
    assert "text/html" in html.headers.get("content-type", "")
    assert b"CaseSifter" in html.content or b"Authenticity" in html.content or b"Smoke" in html.content

    pdf = client.get(f"/api/v1/jobs/{job_id}/export/pdf", headers=headers)
    assert pdf.status_code == 200, pdf.text
    assert "application/pdf" in pdf.headers.get("content-type", "")
    assert pdf.content[:4] == b"%PDF"

    js = client.get(f"/api/v1/jobs/{job_id}/export/json", headers=headers)
    assert js.status_code == 200, js.text
    assert "application/json" in js.headers.get("content-type", "")
    body = js.json()
    assert body.get("job_id") == job_id
    assert "executive_summary" in body


def test_render_pdf_bytes_roundtrip():
    from app.report.generator import render_html, render_pdf_bytes

    job_id = uuid.uuid4().hex
    from app.models import Report

    report = Report.model_validate(_fixture_report(job_id))
    html = render_html(report)
    assert "smoke-exhibit" in html.lower() or "Smoke" in html
    pdf = render_pdf_bytes(report)
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 200
