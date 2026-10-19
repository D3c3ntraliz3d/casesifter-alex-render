"""Seat invites + role assignment for multi-user orgs."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DEV_TOKENS", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-invites")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-session-secret-invites"
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    yield TestClient(mod.app)
    auth_store.reset_auth_store_for_tests()


def _signup_verify_login(http: TestClient, email: str, password: str = "correct-horse") -> None:
    su = http.post(
        "/v1/signup",
        json={"email": email, "password": password, "name": "Owner", "accepted_terms": True},
    )
    assert su.status_code == 200, su.text
    token = su.json().get("dev_verify_token")
    if token:
        assert http.post("/v1/verify-email", json={"token": token}).status_code == 200
    else:
        assert su.json().get("email_verified") is True
    login = http.post("/v1/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text


def test_admin_invite_and_role(client):
    import auth_store as auth
    from sqlalchemy import select

    _signup_verify_login(client, "owner-invite@example.com")

    with auth.session_scope() as s:
        row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "owner-invite@example.com")
        ).scalar_one()
        row.subscription_json = '{"status":"active","plan":"pro","seats":3}'
        s.add(row)

    overview = client.get("/v1/admin/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["account"]["role"] == "owner"

    invite = client.post(
        "/v1/admin/invites",
        json={"email": "member-invite@example.com", "role": "member"},
    )
    assert invite.status_code == 200, invite.text
    invite_token = invite.json().get("invite_token")
    assert invite_token

    su2 = client.post(
        "/v1/signup",
        json={
            "email": "member-invite@example.com",
            "password": "correct-horse",
            "name": "Member",
            "accepted_terms": True,
            "seat_invite_token": invite_token,
        },
    )
    assert su2.status_code == 200, su2.text
    vtok = su2.json().get("dev_verify_token")
    if vtok:
        assert client.post("/v1/verify-email", json={"token": vtok}).status_code == 200
    else:
        assert su2.json().get("email_verified") is True

    with auth.session_scope() as s:
        owner = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "owner-invite@example.com")
        ).scalar_one()
        member = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "member-invite@example.com")
        ).scalar_one()
        assert member.org_id == owner.id
        assert (member.account_role or "") == "member"

    # Re-login as owner (member verify may have replaced the shared test cookie).
    client.post("/v1/logout")
    login = client.post(
        "/v1/login",
        json={"email": "owner-invite@example.com", "password": "correct-horse"},
    )
    assert login.status_code == 200, login.text

    overview2 = client.get("/v1/admin/overview")
    assert overview2.status_code == 200, overview2.text
    emails = {u["email"] for u in overview2.json()["users"]}
    assert "member-invite@example.com" in emails

    member_id = next(
        u["user_id"] for u in overview2.json()["users"] if u["email"] == "member-invite@example.com"
    )

    # Member cannot open admin overview.
    client.post("/v1/logout")
    assert (
        client.post(
            "/v1/login",
            json={"email": "member-invite@example.com", "password": "correct-horse"},
        ).status_code
        == 200
    )
    assert client.get("/v1/admin/overview").status_code == 403

    client.post("/v1/logout")
    assert (
        client.post(
            "/v1/login",
            json={"email": "owner-invite@example.com", "password": "correct-horse"},
        ).status_code
        == 200
    )
    patch = client.patch(f"/v1/admin/users/{member_id}", json={"role": "admin"})
    assert patch.status_code == 200, patch.text
    with auth.session_scope() as s:
        member = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "member-invite@example.com")
        ).scalar_one()
        assert member.account_role == "admin"

    # Admin cannot invite another admin.
    client.post("/v1/logout")
    assert (
        client.post(
            "/v1/login",
            json={"email": "member-invite@example.com", "password": "correct-horse"},
        ).status_code
        == 200
    )
    assert client.get("/v1/admin/overview").status_code == 200
    invite_admin = client.post(
        "/v1/admin/invites",
        json={"email": "second-admin@example.com", "role": "admin"},
    )
    assert invite_admin.status_code == 403

    # Owner can remove the admin seat.
    client.post("/v1/logout")
    assert (
        client.post(
            "/v1/login",
            json={"email": "owner-invite@example.com", "password": "correct-horse"},
        ).status_code
        == 200
    )
    removed = client.delete(f"/v1/admin/users/{member_id}")
    assert removed.status_code == 200, removed.text
    with auth.session_scope() as s:
        member = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "member-invite@example.com")
        ).scalar_one()
        assert member.org_id == member.id
        assert (member.account_role or "") == "owner"

    # Cannot demote the last owner.
    with auth.session_scope() as s:
        owner = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "owner-invite@example.com")
        ).scalar_one()
        owner_id = owner.id
    last = client.patch(f"/v1/admin/users/{owner_id}", json={"role": "member"})
    assert last.status_code == 400


def test_member_forbidden_on_admin_routes(client):
    import auth_store as auth
    from sqlalchemy import select

    _signup_verify_login(client, "solo-owner@example.com")
    with auth.session_scope() as s:
        row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == "solo-owner@example.com")
        ).scalar_one()
        row.subscription_json = '{"status":"active","plan":"pro","seats":2}'
        s.add(row)

    invite = client.post(
        "/v1/admin/invites",
        json={"email": "plain-member@example.com", "role": "member"},
    )
    assert invite.status_code == 200
    token = invite.json()["invite_token"]
    su = client.post(
        "/v1/signup",
        json={
            "email": "plain-member@example.com",
            "password": "correct-horse",
            "name": "Plain",
            "accepted_terms": True,
            "seat_invite_token": token,
        },
    )
    assert su.status_code == 200
    vtok = su.json().get("dev_verify_token")
    if vtok:
        assert client.post("/v1/verify-email", json={"token": vtok}).status_code == 200

    client.post("/v1/logout")
    assert (
        client.post(
            "/v1/login",
            json={"email": "plain-member@example.com", "password": "correct-horse"},
        ).status_code
        == 200
    )
    assert client.get("/v1/admin/overview").status_code == 403
    assert client.get("/v1/admin/audit").status_code == 403
    assert client.post("/v1/admin/invites", json={"email": "x@example.com", "role": "member"}).status_code == 403
    assert client.post("/v1/admin/sessions/revoke-firm").status_code == 403
