"""Firm admin overview stays inside the caller's firm (access-audit pattern)."""

from __future__ import annotations

from app.fact_memory.store import FactMemoryStore, reset_store


def test_job_health_and_members_are_firm_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "admin.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    try:
        firm_a = store.ensure_firm_for_email("owner-a@example.com", name="Firm A")
        firm_b = store.ensure_firm_for_email("owner-b@example.com", name="Firm B")
        matter = store.create_matter(firm_a["firm_id"], name="Matter A", user_id=firm_a["user_id"])
        store.upsert_analysis_job(
            firm_id=firm_a["firm_id"],
            job_id="job-a-1",
            matter_id=matter["id"],
            sha256="a" * 64,
            status="running",
            user_id=firm_a["user_id"],
        )

        members_a = store.list_members(firm_a["firm_id"])
        members_b = store.list_members(firm_b["firm_id"])
        assert any(m["email"] == "owner-a@example.com" for m in members_a)
        assert all(m["email"] != "owner-a@example.com" for m in members_b)

        health_a = store.job_health_summary(firm_a["firm_id"])
        health_b = store.job_health_summary(firm_b["firm_id"])
        assert health_a["total"] >= 1
        assert health_a["by_status"].get("running", 0) >= 1
        assert health_b["total"] == 0

        updated = store.update_firm_settings(
            firm_a["firm_id"],
            name="Firm A Renamed",
            persist_facts=False,
            user_id=firm_a["user_id"],
        )
        assert updated["name"] == "Firm A Renamed"
        assert updated["persist_facts"] is False
        audit = store.list_audit(firm_a["firm_id"])
        assert any(row["action"] == "firm.settings_update" for row in audit)
        assert store.member_role(firm_a["firm_id"], firm_a["user_id"]) == "owner"
        assert store.member_role(firm_b["firm_id"], firm_a["user_id"]) == ""
    finally:
        reset_store()
