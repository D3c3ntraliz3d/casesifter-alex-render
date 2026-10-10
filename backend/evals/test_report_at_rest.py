"""P0-7: report.json must not survive terminal state as plaintext."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

os.environ.setdefault("FACT_MEMORY_MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LTEyMzQ1Njc4OTBhYmNkZWY=")
os.environ.setdefault("APP_ENV", "development")


def _make_report(job_id: str, secret: str):
    from app.models import AuditTrail, Finding, Report

    finding = Finding(
        id="f1",
        category="metadata",
        severity="low",
        title="t",
        explanation=secret,
        why_suspicious="n/a",
        confidence=50,
    )
    return Report(
        job_id=job_id,
        risk_level="green",
        risk_score=0,
        executive_summary="ok",
        findings=[finding],
        audit=AuditTrail(
            scanned_at=datetime.now(timezone.utc),
            analysis_version="test",
            modules_run=[],
            filename="synth.pdf",
            file_size_bytes=10,
            file_type="application/pdf",
            sha256="a" * 64,
        ),
    )


def test_report_json_encrypted_on_write(tmp_path, monkeypatch):
    from app.config import Settings
    import app.config as config_mod
    import app.storage as storage

    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-report-enc")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(storage, "settings", settings)

    from app.fact_memory.encryption import master_fernet

    master_fernet.cache_clear()

    job_id = uuid.uuid4().hex
    from app.models import JobProgress
    from app.storage import (
        job_dir_for_write,
        load_report,
        report_file_is_encrypted,
        save_progress,
        update_job,
    )

    job_dir_for_write(job_id)
    save_progress(
        JobProgress(
            job_id=job_id,
            filename="synth.pdf",
            status="running",
            stage="scan",
            percent=10,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )

    secret = "SECRET-REPORT-QUOTE-XYZ"
    update_job(job_id, status="complete", report=_make_report(job_id, secret))

    path = settings.jobs_dir / job_id / "report.json"
    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert report_file_is_encrypted(path)
    loaded = load_report(job_id)
    assert loaded is not None
    assert any(secret in (f.explanation or "") for f in loaded.findings)
    master_fernet.cache_clear()


def test_persist_terminal_deletes_report_json(tmp_path, monkeypatch):
    from app.config import Settings
    import app.config as config_mod
    import app.ephemeral as ephemeral
    import app.storage as storage

    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-report-enc")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EPHEMERAL_JOBS", "true")
    (tmp_path / "data").mkdir()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(storage, "settings", settings)
    monkeypatch.setattr(ephemeral, "settings", settings)

    from app.fact_memory.encryption import master_fernet

    master_fernet.cache_clear()

    job_id = uuid.uuid4().hex
    from app.models import JobProgress
    from app.storage import job_dir_for_write, save_progress, update_job

    job_dir_for_write(job_id)
    save_progress(
        JobProgress(
            job_id=job_id,
            filename="synth.pdf",
            status="running",
            stage="scan",
            percent=10,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    secret = "SECRET-PERSIST-REPORT"
    update_job(job_id, status="complete", report=_make_report(job_id, secret))
    report_path = settings.jobs_dir / job_id / "report.json"
    assert report_path.is_file()

    ephemeral.maybe_delete_after_terminal(job_id, retain_report=True)

    assert not report_path.exists(), "persisted jobs must not leave report.json on disk"
    for path in (settings.jobs_dir / job_id).rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert secret not in text
    master_fernet.cache_clear()
