"""Recover matter open after FACT_MEMORY_MASTER_KEY loss."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.fact_memory import encryption as enc
from app.fact_memory.store import FactMemoryStore, reset_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    enc.master_fernet.cache_clear()
    s = FactMemoryStore(database_url=f"sqlite:///{tmp_path / 'fm.sqlite3'}")
    yield s
    reset_store()
    enc.master_fernet.cache_clear()


def test_reset_firm_data_key_allows_list_documents(store: FactMemoryStore, monkeypatch):
    firm = store.ensure_firm_for_email("reset@example.com", auth_user_id="u-reset")
    matter = store.create_matter(firm["firm_id"], name="M", user_id=firm["user_id"])
    # Simulate master key loss: wrap is under old key, process now has a new master.
    old = monkeypatch.getenv("FACT_MEMORY_MASTER_KEY") if hasattr(monkeypatch, "getenv") else None
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", Fernet.generate_key().decode())
    enc.master_fernet.cache_clear()
    with pytest.raises(InvalidToken):
        store.list_documents(firm["firm_id"], matter["id"])

    recovered = store.reset_firm_data_key_after_master_loss(
        firm["firm_id"], user_id=firm["user_id"]
    )
    assert recovered["documents_removed"] >= 0
    assert store.list_documents(firm["firm_id"], matter["id"]) == []
    # silence unused
    _ = old
