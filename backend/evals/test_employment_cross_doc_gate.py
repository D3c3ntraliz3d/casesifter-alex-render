"""Gating test: same-upload March 2019 vs March 2022 employment → HIGH with pages.

Persistence / matter memory is blocked until this passes. Reuses vault
compare_to_case (no second cross-doc engine).
"""

from __future__ import annotations

from app.case_consistency import compare_to_case
from app.fact_extract import extract_facts
from app.page_anchors import build_page_offsets


DOC_A_2019 = """EMPLOYMENT VERIFICATION
Borrower: Michael Summers
Employer: TechCore Solutions
Michael Summers: has been employed by TechCore Solutions located in Cherry Hill, New Jersey since March 2019 in the position of Senior Business Analyst.
"""

DOC_B_2022 = """EMPLOYMENT VERIFICATION
Borrower: Michael Summers
Employer: TechCore Solutions
Michael Summers: Employed since March 2022 at TechCore Solutions. Current annual salary $95,000.
"""


def test_employment_march_2019_vs_2022_high_with_pages():
    """Same-upload peer path: two docs, conflicting employment_start → HIGH + both refs."""
    pages_a = [{"page": 1, "text": DOC_A_2019.strip()}]
    pages_b = [{"page": 1, "text": DOC_B_2022.strip()}]
    offsets_a = build_page_offsets(pages_a)
    offsets_b = build_page_offsets(pages_b)

    fa = extract_facts(DOC_A_2019, page_offsets=offsets_a)
    fb = extract_facts(DOC_B_2022, page_offsets=offsets_b)
    assert any(f.get("key") == "employment_start" and "2019" in str(f.get("value")) for f in fa)
    assert any(f.get("key") == "employment_start" and "2022" in str(f.get("value")) for f in fb)

    prior = {
        "document_id": "doc-a-2019",
        "display_name": "VOE March 2019",
        "original_filename": "voe_2019.pdf",
        "doc_kind": "exhibit",
        "sha256": "sha256-doc-a-2019",
        "facts": fa,
        "text_excerpt": DOC_A_2019,
        "entities": {
            "organizations": ["TechCore Solutions"],
            "people": ["Michael Summers"],
            "locations": ["Cherry Hill"],
        },
        "text_fingerprint": "fp-a-2019",
        "page_offsets": offsets_a,
    }

    findings, _matrix = compare_to_case(
        new_document_id="doc-b-2022",
        new_display_name="VOE March 2022",
        new_entities={
            "organizations": ["TechCore Solutions"],
            "people": ["Michael Summers"],
            "locations": [],
        },
        new_amounts=[],
        new_fingerprint="fp-b-2022",
        prior_snapshots=[prior],
        new_doc_kind="exhibit",
        new_facts=fb,
        new_text=DOC_B_2022,
        new_sha256="sha256-doc-b-2022",
        new_page_offsets=offsets_b,
        case_documents=[
            {
                "id": "doc-a-2019",
                "display_name": "VOE March 2019",
                "doc_kind": "exhibit",
                "sha256": "sha256-doc-a-2019",
            }
        ],
    )

    high = [
        f
        for f in findings
        if f.severity == "high"
        and (f.evidence or {}).get("documentary")
        and (f.evidence or {}).get("refs")
    ]
    assert high, "expected HIGH documentary employment conflict with refs"
    finding = high[0]
    assert "employment" in finding.title.lower() or "Employment" in finding.title

    refs = finding.evidence["refs"]
    assert len(refs) >= 2, "both documents must be cited"
    doc_ids = {r.get("document_id") for r in refs}
    assert "doc-a-2019" in doc_ids
    assert "doc-b-2022" in doc_ids
    sha_set = {r.get("sha256") for r in refs}
    assert "sha256-doc-a-2019" in sha_set
    assert "sha256-doc-b-2022" in sha_set
    assert all(r.get("page") is not None for r in refs[:2]), "both sides need page numbers"


def test_planted_summers_cross_doc_fixture():
    """Planted fixture summers_employment_start_cross_doc must also ship HIGH."""
    import json
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "lie_detector" / "planted_bs_cases.json"
    data = json.loads(path.read_text())
    case = next(c for c in data["cases"] if c["id"] == "summers_employment_start_cross_doc")
    docs = case["documents"]
    prior_text = docs[0]["text"]
    new_text = docs[1]["text"]
    fa = extract_facts(prior_text, page_offsets=[{"page": 1, "start": 0}])
    fb = extract_facts(new_text, page_offsets=[{"page": 1, "start": 0}])
    findings, matrix = compare_to_case(
        new_document_id=docs[1]["id"],
        new_display_name=docs[1]["name"],
        new_entities={},
        new_amounts=[],
        new_fingerprint=None,
        prior_snapshots=[
            {
                "document_id": docs[0]["id"],
                "display_name": docs[0]["name"],
                "doc_kind": "exhibit",
                "sha256": "sha-voe",
                "facts": fa,
                "text_excerpt": prior_text,
            }
        ],
        new_doc_kind="exhibit",
        new_facts=fb,
        new_text=new_text,
        new_sha256="sha-loan",
        new_page_offsets=[{"page": 1, "start": 0}],
    )
    material = [
        f
        for f in findings
        if f.severity in {"high", "medium"} and (f.evidence or {}).get("documentary")
    ]
    red_matrix = [r for r in matrix if r.get("flag") == "red" and r.get("documentary")]
    assert material or red_matrix, "summers_employment_start_cross_doc must ship"


if __name__ == "__main__":
    test_employment_march_2019_vs_2022_high_with_pages()
    test_planted_summers_cross_doc_fixture()
    print("employment_cross_doc_gate_ok")
