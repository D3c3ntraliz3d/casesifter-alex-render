"""P0-5 addendum: accounts.json migration preserves SHA-256; login upgrades to argon2id."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sha256_hash(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DEV_TOKENS", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-migrate-secret")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_SINGLE_USER_EMAIL", "admin@libertygroupfunding.com")
    monkeypatch.setenv("CASESIFTER_SINGLE_USER_PASSWORD", "legacy-password-ok")

    salt = secrets.token_hex(8)
    password = "legacy-password-ok"
    accounts = {
        "users": {
            "admin@libertygroupfunding.com": {
                "email": "admin@libertygroupfunding.com",
                "name": "Admin",
                "salt": salt,
                "password_hash": _sha256_hash(password, salt),
                "created_at": time.time(),
                "trial_until": time.time() + 86400 * 14,
                "subscription": {"status": "trialing"},
                "user_id": "adminlegacyuserid01",
                "support_code": "CS-001-002-003",
                "cohort": "beta",
            }
        },
        "sessions": {},
        "trials": {},
        "trial_domains": {},
        "trial_emails": {},
    }
    store_path = tmp_path / "accounts.json"
    store_path.write_text(json.dumps(accounts), encoding="utf-8")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import importlib
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-migrate-secret"
    mod.STORE = store_path
    mod.DATA = tmp_path
    # Trigger migration via ensure.
    mod._ensure_auth_db()
    yield TestClient(mod.app), password
    auth_store.reset_auth_store_for_tests()


def test_migrates_legacy_password_and_upgrades_on_login(client, tmp_path):
    c, password = client
    import auth_store
    from sqlalchemy import select

    store_path = tmp_path / "accounts.json"
    assert store_path.is_file(), "migration must leave accounts.json in place"

    with auth_store.session_scope() as s:
        row = s.execute(
            select(auth_store.CpUser).where(
                auth_store.CpUser.email == "admin@libertygroupfunding.com"
            )
        ).scalar_one()
        assert row.password_algo == "sha256"
        assert row.legacy_salt
        assert row.email_verified is True
        assert row.id == "adminlegacyuserid01"

    # Idempotent re-run: no second row, file still present.
    n = auth_store.migrate_accounts_json(store_path)
    assert n == 0
    assert store_path.is_file()
    assert not (tmp_path / "accounts.json.migrated").exists()

    login = c.post(
        "/v1/login",
        json={"email": "admin@libertygroupfunding.com", "password": password},
    )
    assert login.status_code == 200, login.text
    assert login.json().get("token")

    with auth_store.session_scope() as s:
        row = s.execute(
            select(auth_store.CpUser).where(
                auth_store.CpUser.email == "admin@libertygroupfunding.com"
            )
        ).scalar_one()
        assert row.password_algo == "argon2id"
        assert row.legacy_salt is None
        count = len(s.execute(select(auth_store.CpUser)).scalars().all())
        assert count == 1
