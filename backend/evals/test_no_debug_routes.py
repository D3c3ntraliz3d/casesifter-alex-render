"""P0-1: /api/agent-debug must not exist in analysis mode."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_no_debug_routes_in_analysis_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-key-no-debug")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-no-debug")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from app.config import Settings
    import app.config as config_mod

    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.api.settings", settings)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)

    assert client.get("/api/health").status_code == 200
    for method, path in (
        ("get", "/api/agent-debug"),
        ("post", "/api/agent-debug"),
        ("delete", "/api/agent-debug"),
    ):
        res = getattr(client, method)(path)
        assert res.status_code == 404, (method, path, res.status_code, res.text)

    # Source hygiene: no debug sink strings left in app/frontend trees.
    root = Path(__file__).resolve().parents[2]
    needles = ("agent-debug", "7576/ingest", "X-Debug-Session-Id")
    for rel in ("backend/app", "frontend/src"):
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in needles:
                assert needle not in text, f"{path} still contains {needle!r}"
