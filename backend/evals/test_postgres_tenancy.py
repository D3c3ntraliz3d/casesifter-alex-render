"""P0: auth + two-firm job isolation against an isolated Postgres test DB.

Requires TEST_DATABASE_URL pointing at a *separate* database (not live data).
Never falls back to DATABASE_URL so a Render Shell mistake cannot hit production.

See docs/P0_DEPLOY.md for the exact Render Shell commands to create
`casesifter_p0_eval` and run this file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import time
import uuid

import pytest

pytestmark = pytest.mark.postgres

_SECRET = "unit-test-session-secret-pg-tenancy"


def _cs2(*, user_id: str, email: str, sid: str) -> str:
    issued = int(time.time())
    payload = f"{user_id}|{email}|{sid}|{issued}".encode("utf-8")
    sig = hmac.new(_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"cs2.{b64(payload)}.{b64(sig)}"


def _pg_test_url() -> str | None:
    url = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if not url.startswith("postgresql"):
        return None
    # Refuse obvious live DB names unless explicitly named as an eval DB.
    lower = url.lower()
    if "p0_eval" not in lower and "p0-eval" not in lower:
        pytest.skip(
            "TEST_DATABASE_URL must name an isolated DB containing 'p0_eval' "
            "(refusing to run against live data)"
        )
    return url


def _postgres_reachable(url: str) -> bool:
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(url, future=True, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def pg_url():
    url = _pg_test_url()
    if not url or not _postgres_reachable(url):
        pytest.skip("Postgres test DB not available (set TEST_DATABASE_URL to …/casesifter_p0_eval)")
    return url


def test_two_firm_job_isolation_on_postgres(pg_url, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SESSION_SECRET", _SECRET)
    monkeypatch.setenv("ANALYSIS_API_KEYS", "ops-key-pg-iso")
    monkeypatch.setenv("CLOUD_CONTROL_URL", "https://control.test.local")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-pg-tenancy")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FACT_MEMORY_SQLITE_PATH", raising=False)
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
    monkeypatch.setattr("app.storage.settings", settings)
    monkeypatch.setattr("app.ephemeral.settings", settings)

    email_a = f"pg-a-{uuid.uuid4().hex[:10]}@example.com"
    email_b = f"pg-b-{uuid.uuid4().hex[:10]}@example.com"
    uid_a = "a" + uuid.uuid4().hex[:31]
    uid_b = "b" + uuid.uuid4().hex[:31]
    tok_a = _cs2(user_id=uid_a, email=email_a, sid="s" + uuid.uuid4().hex[:31])
    tok_b = _cs2(user_id=uid_b, email=email_b, sid="s" + uuid.uuid4().hex[:31])

    def _probe(bearer: str):
        if bearer == tok_a:
            return {"email": email_a, "plan": "trial", "active": True, "user_id": uid_a}
        if bearer == tok_b:
            return {"email": email_b, "plan": "trial", "active": True, "user_id": uid_b}
        return None

    monkeypatch.setattr("app.v1_analysis._probe_control_plane_session", _probe)
    monkeypatch.setattr("app.session_auth.session_row_active", lambda _sid: None)

    import importlib
    import app.main as main_mod
    from fastapi.testclient import TestClient

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    ha = {"Authorization": f"Bearer {tok_a}"}
    hb = {"Authorization": f"Bearer {tok_b}"}

    matter = client.post("/api/v1/matters", headers=ha, json={"name": "pg-iso-a"}).json()
    mid = matter["id"]
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    job = client.post(
        "/api/v1/jobs",
        headers=ha,
        files={"file": ("dummy.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"matter_id": mid, "submitted_by": "test"},
    )
    assert job.status_code == 200, job.text
    jid = job.json()["job_id"]

    list_b = client.get("/api/v1/matters", headers=hb)
    assert list_b.status_code == 200
    body_b = list_b.json()
    matters_b = body_b if isinstance(body_b, list) else body_b.get("matters") or []
    assert matters_b == []
    assert client.get(f"/api/v1/matters/{mid}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/jobs/{jid}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/jobs/{jid}/report", headers=hb).status_code == 404
    assert client.delete(f"/api/v1/jobs/{jid}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/jobs/{jid}", headers=ha).status_code == 200
    reset_store()


def test_cp_auth_tables_on_postgres(pg_url, tmp_path, monkeypatch):
    """Control-plane auth_store creates cp_* tables on the isolated Postgres DB."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "control_plane"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    auth_store.init_auth_store(force=True)
    from sqlalchemy import text

    assert auth_store._engine is not None
    with auth_store._engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'cp_%'"
            )
        ).fetchall()
    names = {r[0] for r in rows}
    assert "cp_users" in names
    assert "cp_sessions" in names
    auth_store.reset_auth_store_for_tests()
