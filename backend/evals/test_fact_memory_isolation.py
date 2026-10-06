"""Firm isolation + encryption + persist opt-out for fact memory."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.case_consistency import compare_to_case
from app.fact_extract import extract_facts
from app.fact_memory.store import FactMemoryStore, reset_store


DOC_A = """EMPLOYMENT VERIFICATION
Borrower: Michael Summers
Employer: TechCore Solutions
Michael Summers: has been employed by TechCore Solutions located in Cherry Hill, New Jersey since March 2019 in the position of Senior Business Analyst.
"""

DOC_B = """EMPLOYMENT VERIFICATION
Borrower: Michael Summers
Employer: TechCore Solutions
Michael Summers: Employed since March 2022 at TechCore Solutions. Current annual salary $95,000.
"""


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "fact_memory_test.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    s = FactMemoryStore(database_url=f"sqlite:///{db}")
    yield s
    reset_store()


def test_firm_isolation_cannot_read_other_matters(store: FactMemoryStore):
    a = store.ensure_firm_for_email("firm-a@example.com", name="Firm A")
    b = store.ensure_firm_for_email("firm-b@example.com", name="Firm B")
    assert a["firm_id"] != b["firm_id"]

    matter_a = store.create_matter(a["firm_id"], name="Matter A", user_id=a["user_id"])
    matter_b = store.create_matter(b["firm_id"], name="Matter B", user_id=b["user_id"])

    store.persist_scan(
        firm_id=a["firm_id"],
        matter_id=matter_a["id"],
        job_id="job-a",
        sha256="aaa111",
        original_filename="a.pdf",
        display_name="Doc A",
        doc_type="exhibit",
        filed_date="2024-01-01",
        page_count=1,
        entities={"people": ["Michael Summers"], "organizations": ["TechCore Solutions"]},
        facts=extract_facts(DOC_A),
        findings=[],
        text_fingerprint="fp-a",
        text_excerpt=DOC_A,
        page_offsets=[{"page": 1, "start": 0}],
        user_id=a["user_id"],
    )

    # Firm B cannot see Firm A's matter
    assert store.get_matter(b["firm_id"], matter_a["id"]) is None
    assert store.list_documents(b["firm_id"], matter_a["id"]) == []
    assert store.list_prior_snapshots(b["firm_id"], matter_a["id"]) == []
    assert store.list_findings(b["firm_id"], matter_a["id"]) == []

    # Firm A sees its own
    docs = store.list_documents(a["firm_id"], matter_a["id"])
    assert len(docs) == 1
    assert docs[0]["sha256"] == "aaa111"
    assert docs[0]["original_filename"] == "a.pdf"

    # Identical document on firm B is a separate row — no cross-firm matching
    store.persist_scan(
        firm_id=b["firm_id"],
        matter_id=matter_b["id"],
        job_id="job-b",
        sha256="aaa111",
        original_filename="a.pdf",
        display_name="Doc A",
        doc_type="exhibit",
        filed_date="2024-01-01",
        page_count=1,
        entities={"people": ["Michael Summers"], "organizations": ["TechCore Solutions"]},
        facts=extract_facts(DOC_A),
        findings=[],
        text_fingerprint="fp-a",
        text_excerpt=DOC_A,
        page_offsets=[{"page": 1, "start": 0}],
        user_id=b["user_id"],
    )
    snaps_b = store.list_prior_snapshots(b["firm_id"], matter_b["id"])
    assert len(snaps_b) == 1
    # Firm A still has only its copy
    assert len(store.list_prior_snapshots(a["firm_id"], matter_a["id"])) == 1


def test_cross_session_priors_feed_compare_to_case(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("lawyer@example.com")
    matter = store.create_matter(firm["firm_id"], name="Summers")

    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job1",
        sha256="sha-2019",
        original_filename="voe_2019.pdf",
        display_name="VOE 2019",
        doc_type="exhibit",
        filed_date="2024-01-01",
        page_count=1,
        entities={"people": ["Michael Summers"], "organizations": ["TechCore Solutions"]},
        facts=extract_facts(DOC_A, page_offsets=[{"page": 1, "start": 0}]),
        findings=[],
        text_fingerprint="fp1",
        text_excerpt=DOC_A,
        page_offsets=[{"page": 1, "start": 0}],
        user_id=firm["user_id"],
    )

    priors = store.list_prior_snapshots(firm["firm_id"], matter["id"])
    assert len(priors) == 1
    assert priors[0]["sha256"] == "sha-2019"
    assert any(f.get("key") == "employment_start" for f in priors[0]["facts"])

    fb = extract_facts(DOC_B, page_offsets=[{"page": 1, "start": 0}])
    findings, _ = compare_to_case(
        new_document_id="new-doc",
        new_display_name="VOE 2022",
        new_entities={"people": ["Michael Summers"], "organizations": ["TechCore Solutions"]},
        new_amounts=[],
        new_fingerprint="fp2",
        prior_snapshots=priors,
        new_doc_kind="exhibit",
        new_facts=fb,
        new_text=DOC_B,
        new_sha256="sha-2022",
        new_page_offsets=[{"page": 1, "start": 0}],
    )
    high = [f for f in findings if f.severity == "high" and (f.evidence or {}).get("documentary")]
    assert high
    assert len(high[0].evidence["refs"]) >= 2


def test_persist_facts_opt_out(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("ephemeral@example.com")
    store.set_persist_facts(firm["firm_id"], False)
    matter = store.create_matter(firm["firm_id"], name="Ephemeral matter")
    result = store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-e",
        sha256="sha-e",
        original_filename="x.pdf",
        display_name="X",
        doc_type="exhibit",
        filed_date=None,
        page_count=1,
        entities={},
        facts=[],
        findings=[],
        text_fingerprint=None,
        text_excerpt="hello",
        page_offsets=None,
        user_id=firm["user_id"],
    )
    assert result["persisted"] is False
    assert store.list_documents(firm["firm_id"], matter["id"]) == []


def test_delete_document_marks_findings_stale(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("del@example.com")
    matter = store.create_matter(firm["firm_id"], name="Del")
    r1 = store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="j1",
        sha256="sha-a",
        original_filename="a.pdf",
        display_name="A",
        doc_type="exhibit",
        filed_date=None,
        page_count=1,
        entities={},
        facts=extract_facts(DOC_A),
        findings=[],
        text_fingerprint="a",
        text_excerpt=DOC_A,
        page_offsets=[{"page": 1, "start": 0}],
    )
    doc_a = r1["document_id"]
    priors = store.list_prior_snapshots(firm["firm_id"], matter["id"])
    fb = extract_facts(DOC_B)
    findings, _ = compare_to_case(
        new_document_id="doc-b",
        new_display_name="B",
        new_entities={},
        new_amounts=[],
        new_fingerprint="b",
        prior_snapshots=priors,
        new_doc_kind="exhibit",
        new_facts=fb,
        new_text=DOC_B,
        new_sha256="sha-b",
    )
    finding_dicts = [f.model_dump(mode="json") for f in findings]
    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="j2",
        sha256="sha-b",
        original_filename="b.pdf",
        display_name="B",
        doc_type="exhibit",
        filed_date=None,
        page_count=1,
        entities={},
        facts=fb,
        findings=finding_dicts,
        text_fingerprint="b",
        text_excerpt=DOC_B,
        page_offsets=[{"page": 1, "start": 0}],
    )
    open_before = store.list_findings(firm["firm_id"], matter["id"])
    assert open_before
    assert store.delete_document(firm["firm_id"], doc_a)
    open_after = store.list_findings(firm["firm_id"], matter["id"])
    stale = store.list_findings(firm["firm_id"], matter["id"], include_stale=True)
    assert len(open_after) < len(stale) or all(f["status"] != "open" or doc_a not in str(f) for f in open_after)
    assert any(f["status"] == "stale" for f in stale)


def test_matter_export_and_purge(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("purge@example.com")
    matter = store.create_matter(firm["firm_id"], name="Purge me")
    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="jp",
        sha256="sha-p",
        original_filename="p.pdf",
        display_name="P",
        doc_type="exhibit",
        filed_date=None,
        page_count=1,
        entities={"people": ["Ada"]},
        facts=[{"key": "employment_start", "value": "2019-03", "display": "March 2019", "page": 1}],
        findings=[],
        text_fingerprint="p",
        text_excerpt="x",
        page_offsets=None,
    )
    payload = store.export_matter(firm["firm_id"], matter["id"], user_id=firm["user_id"])
    assert payload["matter"]["id"] == matter["id"]
    assert len(payload["documents"]) == 1
    assert store.delete_matter(firm["firm_id"], matter["id"], user_id=firm["user_id"])
    assert store.get_matter(firm["firm_id"], matter["id"]) is None
    assert store.purge_firm(firm["firm_id"])
    assert store.get_firm(firm["firm_id"]) is None
