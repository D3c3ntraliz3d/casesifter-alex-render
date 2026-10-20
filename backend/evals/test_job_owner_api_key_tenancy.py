"""Ops API keys must not cross-read another firm's analysis jobs."""

from __future__ import annotations

import pytest
from fastapi import HTTPException


def test_api_key_cannot_read_foreign_firm_job(monkeypatch):
    import app.v1_analysis as v1

    monkeypatch.setattr(
        v1,
        "resolve_firm_context",
        lambda _auth: {"firm_id": "firm-ops", "user_id": "ops"},
    )

    class _Store:
        def get_analysis_job(self, job_id):
            return {"job_id": job_id, "firm_id": "firm-other", "status": "complete"}

    monkeypatch.setattr("app.fact_memory.store.get_store", lambda: _Store())
    monkeypatch.setattr("app.storage.JOB_ID_RE", __import__("re").compile(r"^[a-f0-9]{32}$"))

    job_id = "a" * 32
    with pytest.raises(HTTPException) as exc:
        v1.require_job_owner(job_id, {"auth": "api_key", "authenticated": True})
    assert exc.value.status_code == 404


def test_api_key_can_read_ops_firm_job(monkeypatch):
    import app.v1_analysis as v1

    monkeypatch.setattr(
        v1,
        "resolve_firm_context",
        lambda _auth: {"firm_id": "firm-ops", "user_id": "ops"},
    )

    class _Store:
        def get_analysis_job(self, job_id):
            return {"job_id": job_id, "firm_id": "firm-ops", "status": "complete"}

    monkeypatch.setattr("app.fact_memory.store.get_store", lambda: _Store())
    monkeypatch.setattr("app.storage.JOB_ID_RE", __import__("re").compile(r"^[a-f0-9]{32}$"))

    job_id = "b" * 32
    owned = v1.require_job_owner(job_id, {"auth": "api_key", "authenticated": True})
    assert owned["job"]["firm_id"] == "firm-ops"
    assert owned["firm_ctx"]["firm_id"] == "firm-ops"
