"""P0: analysis refuses missing SESSION_SECRET (no default, no probe-only auth)."""

from __future__ import annotations

import pytest


def test_require_session_secret_raises_when_empty(monkeypatch):
    from app.session_auth import require_session_secret_configured

    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        require_session_secret_configured()


def test_require_session_secret_raises_when_whitespace(monkeypatch):
    from app.session_auth import require_session_secret_configured

    monkeypatch.setenv("SESSION_SECRET", "   ")
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        require_session_secret_configured()


def test_production_analysis_boot_requires_session_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-session-secret")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    monkeypatch.setattr(main_mod, "settings", settings)
    # Force analysis mode on the module-level _mode used by startup.
    monkeypatch.setattr(main_mod, "_mode", "analysis")

    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        main_mod._startup()


def test_analysis_rejects_sessions_when_secret_missing(tmp_path, monkeypatch):
    """Even without production boot refuse, empty secret rejects all session bearers."""
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("ANALYSIS_API_KEYS", "ops-key-no-secret")
    monkeypatch.setenv("CLOUD_CONTROL_URL", "https://control.test.local")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-session-secret")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)

    # Probe would succeed — must still 401 without SESSION_SECRET.
    monkeypatch.setattr(
        "app.v1_analysis._probe_control_plane_session",
        lambda _t: {"email": "a@example.com", "plan": "trial", "active": True},
    )

    import importlib
    import app.main as main_mod
    from fastapi.testclient import TestClient

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    res = client.get("/api/v1/matters", headers={"Authorization": "Bearer anything"})
    assert res.status_code == 401
    # API key still works.
    assert client.get(
        "/api/v1/matters", headers={"X-API-Key": "ops-key-no-secret"}
    ).status_code == 200
