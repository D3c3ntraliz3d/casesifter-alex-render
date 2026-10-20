"""Production analysis must not fall back to ephemeral SQLite."""

from __future__ import annotations

import pytest

from app.fact_memory.models import database_url_from_env, fact_memory_backend_label


def test_production_analysis_requires_database_url(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "should-not-use.sqlite3"))
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        database_url_from_env()


def test_local_dev_allows_sqlite_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db = tmp_path / "fm.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    url = database_url_from_env()
    assert url.startswith("sqlite:///")
    assert fact_memory_backend_label() == "sqlite"


def test_postgres_url_normalized(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@host/db")
    url = database_url_from_env()
    assert url.startswith("postgresql+psycopg://")
    assert fact_memory_backend_label() == "postgres"
