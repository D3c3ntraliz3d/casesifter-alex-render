"""Source bytes are deleted after a scan. Fact rows are a separate store."""

from __future__ import annotations

from pathlib import Path

from app.ephemeral import delete_ephemeral_source


def test_delete_ephemeral_source_removes_upload_and_keeps_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("EPHEMERAL_JOBS", "true")
    from app.config import Settings
    import app.config as config_mod
    import app.ephemeral as ephemeral_mod

    settings = Settings(data_dir=tmp_path / "data", ephemeral_jobs=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(ephemeral_mod, "settings", settings)
    monkeypatch.setattr("app.storage.settings", settings)

    job_id = "abc123" + ("0" * 26)
    root = settings.jobs_dir / job_id
    root.mkdir(parents=True)
    upload = root / "exhibit.pdf"
    upload.write_bytes(b"%PDF-1.4 not a real document")
    (root / "progress.json").write_text("{}", encoding="utf-8")
    (root / "artifacts").mkdir()
    (root / "artifacts" / "page.png").write_bytes(b"png")

    delete_ephemeral_source(job_id)

    assert not upload.exists()
    assert not (root / "artifacts").exists()
    assert (root / "progress.json").is_file()
    assert Path(root / "progress.json").read_text(encoding="utf-8") == "{}"
