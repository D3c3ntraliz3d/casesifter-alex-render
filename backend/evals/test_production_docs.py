"""Production analysis API does not publish OpenAPI docs."""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from app.edge_security import docs_kwargs


def test_docs_kwargs_hide_schemas_in_production():
    hidden = docs_kwargs("production")
    assert hidden == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    shown = docs_kwargs("development")
    assert shown["docs_url"] == "/docs"


def test_production_app_has_no_docs(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-key-docs")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-docs")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from app.config import Settings
    import app.config as config_mod

    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.api.settings", settings)

    import app.main as main_mod

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    body = client.get("/").json()
    assert "docs" not in body
    assert client.get("/api/health").status_code == 200
