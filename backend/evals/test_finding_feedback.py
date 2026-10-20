"""Firm soft-suppress feedback on findings — scoped, non-destructive."""

from __future__ import annotations

import json

import pytest


def test_finding_feedback_fingerprint_stable():
    from app.case_consistency import finding_feedback_fingerprint

    a = {
        "title": "Service address conflict",
        "finding_type": "gotcha",
        "evidence": {
            "field": "service_address",
            "claim_quote": "I live at 123 Main",
            "evidence_quote": "Service at 456 Oak",
            "analysis_type": "gotcha",
        },
    }
    b = {
        **a,
        "id": "different-id",
        "evidence": {**a["evidence"]},
    }
    assert finding_feedback_fingerprint(a) == finding_feedback_fingerprint(b)
    assert len(finding_feedback_fingerprint(a)) == 40


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-finding-fb")
    db = tmp_path / "fact_memory_fb.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.fact_memory.store import FactMemoryStore, reset_store

    reset_store()
    s = FactMemoryStore(database_url=f"sqlite:///{db}")
    yield s
    reset_store()


def _seed_finding(store, firm_id: str, matter_id: str) -> str:
    from app.fact_memory import encryption as enc
    from app.fact_memory.models import FindingRow
    from app.fact_memory.store import _new_id, _utcnow

    with store.session() as s:
        fernet = store._fernet(s, firm_id)
        evidence = {
            "field": "service_address",
            "claim_quote": "I reside at 1739 46th",
            "evidence_quote": "Service at first floor",
            "analysis_type": "gotcha",
            "case_impact": "material_review",
            "case_impact_bucket": "element",
            "gotcha": True,
        }
        fid = _new_id()
        s.add(
            FindingRow(
                id=fid,
                matter_id=matter_id,
                firm_id=firm_id,
                finding_type="gotcha",
                severity="high",
                confidence=90.0,
                description_enc=enc.encrypt_text(fernet, "Address conflict"),
                title_enc=enc.encrypt_text(fernet, "Service address conflict"),
                fact_ids_json=json.dumps([]),
                evidence_enc=enc.encrypt_json(fernet, evidence),
                status="open",
                created_at=_utcnow(),
            )
        )
        s.commit()
        return fid


def test_finding_feedback_store_scoped(store):
    from app.case_consistency import finding_feedback_fingerprint
    from app.fact_memory.matters_api import _case_impact_board

    a = store.ensure_firm_for_email("firm-a-fb@example.com", name="Firm A")
    b = store.ensure_firm_for_email("firm-b-fb@example.com", name="Firm B")
    matter = store.create_matter(a["firm_id"], name="Matter A", user_id=a["user_id"])
    fid = _seed_finding(store, a["firm_id"], matter["id"])

    finding = store.get_finding(a["firm_id"], matter["id"], fid)
    assert finding is not None
    fp = finding_feedback_fingerprint(finding)

    # Cross-firm matter write fails.
    with pytest.raises(KeyError):
        store.set_finding_feedback(
            b["firm_id"],
            matter["id"],
            fingerprint=fp,
            status="false_positive",
            finding_id=fid,
        )

    fb = store.set_finding_feedback(
        a["firm_id"],
        matter["id"],
        fingerprint=fp,
        status="false_positive",
        finding_id=fid,
        user_id=a["user_id"],
    )
    assert fb["status"] == "false_positive"
    mapped = store.list_finding_feedback(a["firm_id"], matter["id"])
    assert mapped[fp]["status"] == "false_positive"
    assert mapped[f"id:{fid}"]["status"] == "false_positive"
    # Firm B sees nothing for this matter.
    assert store.list_finding_feedback(b["firm_id"], matter["id"]) == {}

    board = _case_impact_board(store, a["firm_id"], matter["id"])
    assert board["ignored_count"] >= 1
    annotated = next(f for f in board["findings"] if f["id"] == fid)
    assert annotated["user_feedback"]["status"] == "false_positive"
    assert annotated.get("title")
    assert annotated.get("evidence")

    store.set_finding_feedback(
        a["firm_id"],
        matter["id"],
        fingerprint=fp,
        status="confirmed",
        finding_id=fid,
    )
    store.set_finding_feedback(
        a["firm_id"],
        matter["id"],
        fingerprint=fp,
        status="clear",
        finding_id=fid,
    )
    assert store.list_finding_feedback(a["firm_id"], matter["id"]) == {}
    # Evidence row still open — soft-suppress only.
    still = store.get_finding(a["firm_id"], matter["id"], fid)
    assert still is not None
    assert still["status"] == "open"


def test_finding_feedback_api(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-key-fb")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-finding-fb-api")
    db = tmp_path / "fm_fb_api.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    from app.fact_memory.store import FactMemoryStore, reset_store
    from app.config import Settings
    import app.config as config_mod
    import importlib
    from fastapi.testclient import TestClient

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)

    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    monkeypatch.setattr("app.fact_memory.store.get_store", lambda: store)
    monkeypatch.setattr("app.fact_memory.matters_api.get_store", lambda: store)
    monkeypatch.setattr("app.v1_analysis.get_store", lambda: store, raising=False)

    # API keys resolve to ops@casesifter.local firm.
    ops = store.ensure_firm_for_email("ops@casesifter.local", name="Ops / API key")
    matter = store.create_matter(ops["firm_id"], name="Ops Matter", user_id=ops["user_id"])
    fid = _seed_finding(store, ops["firm_id"], matter["id"])

    import app.main as main_mod

    importlib.reload(main_mod)
    monkeypatch.setattr("app.fact_memory.store.get_store", lambda: store)
    monkeypatch.setattr("app.fact_memory.matters_api.get_store", lambda: store)

    client = TestClient(main_mod.app)
    headers = {"X-API-Key": "test-key-fb"}

    posted = client.post(
        f"/api/v1/matters/{matter['id']}/findings/{fid}/feedback",
        headers=headers,
        json={"status": "false_positive"},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["user_feedback"]["status"] == "false_positive"

    listed = client.get(f"/api/v1/matters/{matter['id']}/findings", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    finding = next(f for f in body.get("findings") or [] if f.get("id") == fid)
    assert finding["user_feedback"]["status"] == "false_positive"
    assert body.get("ignored_count", 0) >= 1

    cleared = client.post(
        f"/api/v1/matters/{matter['id']}/findings/{fid}/feedback",
        headers=headers,
        json={"status": "clear"},
    )
    assert cleared.status_code == 200
    assert cleared.json()["user_feedback"]["status"] is None
