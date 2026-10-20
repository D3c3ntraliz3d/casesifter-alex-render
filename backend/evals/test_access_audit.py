"""Access log rows stay inside the firm that produced them."""

from __future__ import annotations

from app.fact_memory.store import FactMemoryStore, reset_store


def test_export_audit_is_not_visible_to_another_firm(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "audit.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    try:
        firm_a = store.ensure_firm_for_email("a@example.com", name="A")
        firm_b = store.ensure_firm_for_email("b@example.com", name="B")
        matter = store.create_matter(firm_a["firm_id"], name="Matter A", user_id=firm_a["user_id"])
        store.export_matter(
            firm_a["firm_id"],
            matter["id"],
            user_id=firm_a["user_id"],
            request_id="req-export-1",
        )
        try:
            store.export_matter(firm_b["firm_id"], matter["id"], user_id=firm_b["user_id"])
        except KeyError:
            pass
        else:
            raise AssertionError("cross-firm export should fail")

        own = store.list_audit(firm_a["firm_id"])
        other = store.list_audit(firm_b["firm_id"])
        assert any(row["action"] == "matter.export" and row["request_id"] == "req-export-1" for row in own)
        assert all(row["firm_id"] == firm_a["firm_id"] for row in own)
        assert not any(row["action"] == "matter.export" for row in other)
        assert store.member_role(firm_a["firm_id"], firm_a["user_id"]) == "owner"
        assert store.member_role(firm_b["firm_id"], firm_a["user_id"]) == ""
    finally:
        reset_store()
