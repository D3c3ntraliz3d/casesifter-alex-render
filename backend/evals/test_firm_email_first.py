"""Email-stable firm identity + matter reclaim (bulk Matter-not-found fix)."""

from __future__ import annotations

import pytest

from app.fact_memory.store import FactMemoryStore, reset_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "firm_email_first.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    s = FactMemoryStore(database_url=f"sqlite:///{db}")
    yield s
    reset_store()


def test_email_preferred_over_divergent_auth_id(store: FactMemoryStore):
    email = "lawyer@example.com"
    email_firm = store.ensure_firm_for_email(email, name="Email Firm")
    matter = store.create_matter(email_firm["firm_id"], name="Open Matter", user_id=email_firm["user_id"])

    # Orphan auth_id row on a different empty firm (simulates control-id drift).
    orphan = store.ensure_firm_for_email("orphan-temp@example.com", auth_user_id="control-user-aaa")
    assert orphan["firm_id"] != email_firm["firm_id"]

    # Re-enter as the real email with that auth_id — must stay on the email firm.
    again = store.ensure_firm_for_email(email, auth_user_id="control-user-aaa")
    assert again["firm_id"] == email_firm["firm_id"]
    assert store.get_matter(again["firm_id"], matter["id"]) is not None


def test_reclaim_access_after_detach(store: FactMemoryStore):
    owner = store.ensure_firm_for_email("owner@example.com", name="Owner Firm")
    member = store.ensure_firm_for_email(
        "member@example.com",
        auth_user_id="member-auth-1",
        join_owner_email="owner@example.com",
        account_role="member",
    )
    assert member["firm_id"] == owner["firm_id"]
    matter = store.create_matter(owner["firm_id"], name="Shared", user_id=member["user_id"])

    detached = store.detach_member(owner["firm_id"], member["user_id"], actor_id=owner["user_id"])
    assert detached["new_firm_id"] != owner["firm_id"]
    assert store.get_matter(detached["new_firm_id"], matter["id"]) is None

    recovered = store.reclaim_access_to_matter(
        matter["id"],
        email="member@example.com",
        auth_user_id="member-auth-1",
    )
    assert recovered is not None
    assert recovered["firm_id"] == owner["firm_id"]
    assert store.get_matter(recovered["firm_id"], matter["id"]) is not None
