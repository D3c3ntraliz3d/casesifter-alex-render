"""Finished jobs keep compact module lights on progress.json."""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.models import AuditTrail, JobProgress, ModuleResult, Report
from app.storage import create_job, load_progress, update_job


def test_update_job_stores_scan_modules(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", ephemeral_jobs=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.storage.settings", settings)

    job_id = "a" * 32
    create_job(job_id, "exhibit.pdf")
    now = datetime.now(timezone.utc)
    report = Report(
        job_id=job_id,
        risk_level="green",
        risk_score=10,
        executive_summary="ok",
        findings=[],
        timeline=[],
        modules=[
            ModuleResult(name="metadata", status="ok", duration_ms=1, summary="0 finding(s)"),
            ModuleResult(
                name="image_forensics",
                status="ok",
                duration_ms=90,
                summary="0 finding(s)",
                extras={},
            ),
            ModuleResult(name="ocr_text", status="ok", duration_ms=1, summary="0 finding(s)"),
        ],
        audit=AuditTrail(
            scanned_at=now,
            analysis_version="test",
            modules_run=["metadata", "image_forensics", "ocr_text"],
            filename="exhibit.pdf",
            file_size_bytes=10,
            file_type="application/pdf",
            sha256="b" * 64,
        ),
    )
    update_job(job_id, status="complete", stage="generating_report", percent=100, report=report)
    progress = load_progress(job_id)
    assert isinstance(progress, JobProgress)
    names = {m["name"]: m["status"] for m in (progress.scan_modules or [])}
    assert names["image_forensics"] == "ok"
    assert names["metadata"] == "ok"
