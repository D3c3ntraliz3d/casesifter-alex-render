"""Phase 0 / Phase 1 smoke tests — APP_MODE gating + page-anchored refs."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_analysis_mode_hides_vault_and_requires_v1_key(monkeypatch):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-key-abc")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    # Re-import settings + app with the new env.
    from app.config import Settings

    settings = Settings()
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.api.settings", settings)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json().get("app_mode") == "analysis"

    cases = client.get("/api/cases")
    assert cases.status_code == 404

    no_key = client.post("/api/v1/jobs")
    assert no_key.status_code == 401

    bad_key = client.post("/api/v1/jobs", headers={"X-API-Key": "wrong"})
    assert bad_key.status_code == 401


def test_page_offsets_and_fact_anchors():
    from app.page_anchors import build_page_offsets, char_to_page, enrich_fact_anchors
    from app.fact_extract import extract_facts
    from app.case_consistency import compare_to_case

    pages = [
        {"page": 1, "text": "Employee: Alex Summers\nEmployed since March 2019 at TechCore."},
        {"page": 2, "text": "Verification of employment.\nStart date: March 2022."},
    ]
    offsets = build_page_offsets(pages)
    full = "\n".join(p["text"] for p in pages)
    assert char_to_page(0, offsets) == 1
    assert char_to_page(len(pages[0]["text"]) + 2, offsets) == 2

    facts = extract_facts(full, page_offsets=offsets)
    assert any(f.get("page") for f in facts)
    anchored = enrich_fact_anchors(list(facts), full, offsets)
    assert all(
        (f.get("page") is not None) or not f.get("quote") for f in anchored
    )

    # Cross-doc finding should carry refs with pages on both sides.
    a_text = "Employee: Alex Summers\nEmployed since March 2019 at TechCore."
    b_text = "Employee: Alex Summers\nVerification of employment. Start date: March 2022."
    fa = extract_facts(a_text, page_offsets=[{"page": 1, "start": 0}])
    fb = extract_facts(b_text, page_offsets=[{"page": 1, "start": 0}])
    doc_a = {
        "document_id": "a",
        "display_name": "VOE",
        "doc_kind": "exhibit",
        "sha256": "aaa",
        "facts": fa,
        "text_excerpt": a_text,
        "entities": {"organizations": ["TechCore"], "people": ["Alex Summers"], "locations": []},
        "text_fingerprint": "fp-a",
    }
    findings, _ = compare_to_case(
        new_document_id="b",
        new_display_name="Loan app",
        new_entities={"organizations": ["TechCore"], "people": ["Alex Summers"], "locations": []},
        new_amounts=[],
        new_fingerprint="fp-b",
        prior_snapshots=[doc_a],
        new_doc_kind="exhibit",
        new_facts=fb,
        new_text=b_text,
        new_sha256="bbb",
        new_page_offsets=[{"page": 1, "start": 0}],
    )
    cross = [
        f
        for f in findings
        if (f.evidence or {}).get("documentary") and (f.evidence or {}).get("refs")
    ]
    assert cross, "expected a documentary cross-doc finding with refs"
    refs = cross[0].evidence["refs"]
    assert len(refs) >= 2
    assert refs[0].get("page") is not None
    assert refs[1].get("page") is not None
    assert refs[0].get("sha256") == "bbb"
    assert refs[1].get("sha256") == "aaa"


if __name__ == "__main__":
    import os
    import sys

    # Minimal runner without pytest dependency for APP_MODE import isolation.
    from app.page_anchors import build_page_offsets, char_to_page
    from app.fact_extract import extract_facts
    from app.case_consistency import compare_to_case

    test_page_offsets_and_fact_anchors()
    print("page_anchors_ok")

    os.environ["APP_MODE"] = "analysis"
    os.environ["APP_ENV"] = "development"
    os.environ["ANALYSIS_API_KEYS"] = "test-key-abc"
    os.environ["CORS_ALLOW_ORIGINS"] = "https://casesifter.com"

    # Fresh process-style check via TestClient after reloading modules.
    import importlib
    from app import config as config_mod

    importlib.reload(config_mod)
    import app.main as main_mod
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/cases").status_code == 404
    assert client.post("/api/v1/jobs").status_code == 401
    print("app_mode_ok")
    sys.exit(0)
