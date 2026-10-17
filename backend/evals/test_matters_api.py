"""API smoke: matters CRUD + jobs accept matter_id (auth via API key)."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient


def test_content_disposition_keeps_section_sign():
    from app.fact_memory.store import content_disposition_inline

    header = content_disposition_inline(
        "Exhibit 98 — C (Motion #4) CEMA §202.5(e)",
        "application/pdf",
    )
    assert header.startswith("inline;")
    assert "filename*=UTF-8''" in header
    assert "%C2%A7" in header or "%A7" in header
    assert header.endswith(".pdf") or ".pdf" in header.split("filename*=", 1)[-1]


def test_resolve_upload_labels_exhibit_parens_s():
    """Hosted path must not leave EXHIBIT(S) packets as Unknown."""
    from app.v1_analysis import _resolve_upload_labels

    fname = "111 - EXHIBIT(S) - P (Motion #4) Settlement Conference Aff.pdf"
    name, kind = _resolve_upload_labels(fname, fname, None)
    assert kind == "exhibit"
    assert name.startswith("Exhibit 111")
    assert "Settlement Conference Aff" in name

    bare = "32 - EXHIBIT(S).pdf"
    name2, kind2 = _resolve_upload_labels(bare, bare, None)
    assert kind2 == "exhibit"
    assert name2 == "Exhibit 32"

    # Explicit user kind still wins for non-exhibit names.
    name3, kind3 = _resolve_upload_labels("voe.txt", "VOE 2019", "affidavit")
    assert kind3 == "affidavit"
    assert name3 == "VOE 2019"


def test_resolve_upload_labels_handoff_affirmation_affidavit():
    """Handoff filenames: AFFIRMATION AFFIDAVIT must not resolve to Unknown."""
    from app.v1_analysis import _resolve_upload_labels

    aos = "17 AFFIRMATION AFFIDAVIT OF SERVICE.pdf"
    name, kind = _resolve_upload_labels(aos, aos, None)
    assert kind == "proof_of_service"
    assert "affidavit" in name.lower() or "service" in name.lower()

    nyscef = "533445_2023_Specialized_Loan_Servi_AFFIRMATION_AFFIDAV_17.pdf"
    name2, kind2 = _resolve_upload_labels(nyscef, nyscef, None)
    assert kind2 in {"proof_of_service", "affidavit", "affirmation"}
    assert kind2 is not None

    support = "62 AFFIDAVIT OR AFFIRMATION IN SUPPORT OF MOTION.pdf"
    name3, kind3 = _resolve_upload_labels(support, support, None)
    assert kind3 in {"affidavit", "affirmation", "motion"}
    assert kind3 is not None
    assert name3

    bare_pair = "AFFIRMATION AFFIDAVIT.pdf"
    name4, kind4 = _resolve_upload_labels(bare_pair, bare_pair, None)
    assert kind4 in {"affidavit", "affirmation"}
    assert "Affidavit" in name4 or "Affirmation" in name4


def test_matters_crud_and_job_matter_id(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-key-matter")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "test-master-key-matters-api")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.ephemeral.settings", settings)
    # Every already-imported module that bound the old Settings at import time
    # (app.storage, scan executors, …) must see this test's DATA_DIR too —
    # otherwise the parent writes the job under a previous test's data dir while
    # the spawned scan child (fresh env) can't find it ("Unknown job").
    import sys

    for name, mod in list(sys.modules.items()):
        if not (name == "app" or name.startswith("app.") or name.startswith("worker.")):
            continue
        current = getattr(mod, "settings", None)
        if current is not None and current is not settings and current.__class__.__name__ == "Settings":
            monkeypatch.setattr(mod, "settings", settings)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    headers = {"X-API-Key": "test-key-matter"}

    created = client.post(
        "/api/v1/matters",
        headers=headers,
        json={"name": "U.S. Bank v. Summers", "index_number": "123/2024"},
    )
    assert created.status_code == 200, created.text
    matter = created.json()
    matter_id = matter["id"]

    listed = client.get("/api/v1/matters", headers=headers)
    assert listed.status_code == 200
    assert any(m["id"] == matter_id for m in listed.json()["matters"])

    detail = client.get(f"/api/v1/matters/{matter_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["documents"] == []

    # Upload without waiting for full pipeline — just ensure matter_id is accepted.
    # Use a tiny text file (allowed) so pipeline can run quickly.
    content = b"Employee: Michael Summers\nEmployed since March 2019 at TechCore.\n"
    files = {"file": ("voe.txt", io.BytesIO(content), "text/plain")}
    data = {
        "matter_id": matter_id,
        "display_name": "VOE 2019",
        "doc_kind": "exhibit",
        "submitted_by": "test",
        "filed_date": "2024-01-15",
    }
    job = client.post("/api/v1/jobs", headers=headers, files=files, data=data)
    assert job.status_code == 200, job.text
    job_id = job.json()["job_id"]
    assert job_id

    # Poll briefly for completion (OCR/pipeline on txt is fast).
    import time

    status = None
    for _ in range(60):
        prog = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert prog.status_code == 200
        status = prog.json()
        if status["status"] in {"complete", "failed"}:
            break
        time.sleep(0.5)
    assert status and status["status"] == "complete", status
    assert status.get("persist_facts") is True

    # Source PDF/txt must be gone; report may remain for persist firms.
    from pathlib import Path
    from app.storage import job_dir

    root = job_dir(job_id)
    leftovers = [
        p.name
        for p in root.iterdir()
        if p.is_file() and p.name.lower() in {"voe.txt"} or (p.suffix.lower() in {".txt", ".docx"} and not p.name.startswith("report"))
    ]
    # Source upload must be gone; generated report.pdf/html/json may remain for persist firms.
    # Status flips to complete inside the pipeline; source cleanup runs in the
    # runner's finally right after — allow that brief window before asserting.
    for _ in range(40):
        if not (root / "voe.txt").exists():
            break
        time.sleep(0.05)
    assert not (root / "voe.txt").exists(), "source upload must be deleted after run"

    refreshed = client.get(f"/api/v1/matters/{matter_id}", headers=headers)
    assert refreshed.status_code == 200
    docs = refreshed.json()["documents"]
    assert len(docs) >= 1
    assert docs[0].get("filed_date") == "2024-01-15" or docs[0].get("sha256")

    source = client.get(
        f"/api/v1/matters/{matter_id}/documents/{docs[0]['id']}/file",
        headers=headers,
    )
    assert source.status_code == 410, source.text

    refused = client.post(
        f"/api/v1/matters/{matter_id}/documents/{docs[0]['id']}/file",
        headers=headers,
    )
    assert refused.status_code == 410

    # filed_date edit
    patched = client.patch(
        f"/api/v1/matters/{matter_id}/documents/{docs[0]['id']}",
        headers=headers,
        json={"filed_date": "2023-07-01"},
    )
    assert patched.status_code == 200
    assert patched.json()["filed_date"] == "2023-07-01"

    exported = client.get(f"/api/v1/matters/{matter_id}/export", headers=headers)
    assert exported.status_code == 200
    body = exported.json()
    assert body["matter"]["id"] == matter_id
    assert body.get("export_kind") == "matter_review_pack"
    assert "case_impact" in body
    assert "document_summary" in body
    assert isinstance(body["case_impact"].get("gotchas"), list)
    assert isinstance(body["document_summary"], list)

    deleted = client.delete(f"/api/v1/matters/{matter_id}", headers=headers)
    assert deleted.status_code == 200
