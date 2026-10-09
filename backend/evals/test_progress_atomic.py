"""progress.json atomic write + load_progress partial-read tolerance."""

from __future__ import annotations

import json

from app.models import JobProgress
from app.storage import create_job, load_progress, save_progress, update_job, utcnow


def test_load_progress_retries_bad_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.config import Settings
    import app.config as config_mod
    import app.storage as storage_mod

    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(storage_mod, "settings", settings)

    create_job("job-atomic", "demo.pdf")
    path = storage_mod._progress_path("job-atomic")
    # Simulate a torn write.
    path.write_text('{"job_id": "job-atomic", "status":', encoding="utf-8")
    assert load_progress("job-atomic") is None

    # Valid rewrite via atomic helper recovers.
    progress = JobProgress(
        job_id="job-atomic",
        status="queued",
        stage="queued",
        percent=0,
        message="ok",
        filename="demo.pdf",
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    save_progress(progress)
    loaded = load_progress("job-atomic")
    assert loaded is not None
    assert loaded.status == "queued"

    update_job("job-atomic", status="running", stage="ingesting", percent=5)
    again = load_progress("job-atomic")
    assert again is not None
    assert again.status == "running"
    # File is valid JSON after update.
    json.loads(path.read_text(encoding="utf-8"))
