"""Differential routing: restore-2026-09-22-detector (3c41141) vs HEAD.

HEAD must match 9-22 on EXHIBIT forensic, and strictly beat 9-22 on captioned
scan-heavy / blank-image packets that used to take the court_filing fast path.

Usage (from backend/):
  PYTHONPATH=. python3 -m evals.diff_922_detector
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _load_922_scan_profile():
    root = Path(__file__).resolve().parents[2]
    raw = subprocess.check_output(
        ["git", "show", "3c41141:backend/app/scan_profile.py"],
        cwd=root,
    )
    with tempfile.NamedTemporaryFile("wb", suffix="_scan_profile_922.py", delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    spec = importlib.util.spec_from_file_location("scan_profile_922", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def evaluate() -> dict:
    from app.auto_title import propose_document_title
    from app.case_consistency import compare_to_case, fact_conflicts_from_packet
    from app.engines.template_fabrication import find_template_leftovers
    from app.fact_extract import extract_facts
    from app.finding_policy import is_material_finding
    from app.report.scoring import score_risk
    from app.scan_profile import (
        PROFILE_COURT_FILING,
        PROFILE_FORENSIC,
        finalize_scan_profile,
        resolve_scan_profile,
    )

    old = _load_922_scan_profile()

    caption = (
        "FILED: KINGS COUNTY CLERK 01/15/2024 INDEX NO. 533445/2023\n"
        "NYSCEF DOC. NO. 83\n"
        "SUPREME COURT OF THE STATE OF NEW YORK COUNTY OF KINGS\n"
        "Plaintiffs - against -\n"
        "WHEREFORE plaintiff demands judgment."
    )
    # Caption + statement: 9-22 treated pleading caption as court_filing even with bill text.
    statement = (
        caption
        + "\nAccount number 987\nStatement date 01/01/2020\n"
        + "Amount due $130.00\nNational Grid"
    )
    origin_scan = {"guess": "hybrid", "scan_heavy": True, "raster_pages": 1, "text_pages": 1}
    origin_native = {"guess": "native-text", "scan_heavy": False, "raster_pages": 0, "text_pages": 1}

    rows: list[dict] = []

    def row(name: str, old_prof: str, new_prof: str, *, head_must_win: bool = False) -> None:
        head_wins = old_prof == PROFILE_COURT_FILING and new_prof == PROFILE_FORENSIC
        both_forensic = old_prof == PROFILE_FORENSIC and new_prof == PROFILE_FORENSIC
        both_filing = old_prof == PROFILE_COURT_FILING and new_prof == PROFILE_COURT_FILING
        if head_must_win:
            ok = head_wins
        else:
            ok = both_forensic or both_filing or head_wins
        rows.append(
            {
                "case": name,
                "profile_922": old_prof,
                "profile_head": new_prof,
                "head_wins_scan": head_wins,
                "both_forensic": both_forensic,
                "ok": ok,
            }
        )

    _name, kind = propose_document_title("EXHIBIT_S__83.pdf", caption)
    row(
        "exhibit_filename_scan",
        old.resolve_scan_profile(kind, caption),
        finalize_scan_profile(
            resolve_scan_profile(kind, caption),
            doc_kind=kind,
            peek_text=caption,
            suffix=".pdf",
            origin=origin_scan,
        ),
    )

    row(
        "blank_statement_with_caption_scan",
        old.resolve_scan_profile("", statement),
        finalize_scan_profile(
            resolve_scan_profile("", statement),
            doc_kind="",
            peek_text=statement,
            suffix=".pdf",
            origin=origin_scan,
        ),
        head_must_win=True,
    )

    row(
        "blank_caption_scan_heavy",
        old.resolve_scan_profile("", caption),
        finalize_scan_profile(
            resolve_scan_profile("", caption),
            doc_kind="",
            peek_text=caption,
            suffix=".pdf",
            origin=origin_scan,
        ),
        head_must_win=True,
    )

    row(
        "blank_caption_native",
        old.resolve_scan_profile("", caption),
        finalize_scan_profile(
            resolve_scan_profile("", caption),
            doc_kind="",
            peek_text=caption,
            suffix=".pdf",
            origin=origin_native,
        ),
    )

    row(
        "blank_image_empty_peek",
        old.resolve_scan_profile("", ""),
        finalize_scan_profile(
            resolve_scan_profile("", ""),
            doc_kind="",
            peek_text="",
            suffix=".png",
            origin=None,
        ),
        head_must_win=True,
    )

    d22 = (
        "AFFIDAVIT OF WAYNE ALEXANDER\n"
        "I, Wayne Alexander, being duly sworn, state:\n"
        "I have occupied the Subject Property continuously as my primary residence "
        "since April 3, 2019.\n"
    )
    d69 = (
        "AFFIDAVIT OF WAYNE ALEXANDER\n"
        "I, Wayne Alexander, being duly sworn, state:\n"
        "I entered into occupancy of the Subject Property on May 17, 2019.\n"
    )
    hits = fact_conflicts_from_packet(
        [
            {"document_id": "d22", "display_name": "Wayne D22", "text_excerpt": d22},
            {"document_id": "d69", "display_name": "Wayne D69", "text_excerpt": d69},
        ]
    )
    occ = [
        h
        for h in hits
        if h.get("field") == "occupancy_start" or "Occupancy" in str(h.get("title") or "")
    ]
    peer_facts = extract_facts(d22)
    new_facts = extract_facts(d69)
    consistency, _matrix = compare_to_case(
        new_document_id="d69",
        new_display_name="Wayne D69",
        new_entities={},
        new_amounts=[],
        new_fingerprint="x",
        prior_snapshots=[
            {
                "document_id": "d22",
                "display_name": "Wayne D22",
                "facts": peer_facts,
                "text_excerpt": d22,
                "doc_kind": "affidavit",
            }
        ],
        new_doc_kind="affidavit",
        new_facts=new_facts,
        new_text=d69,
        case_documents=[],
    )
    risk, score = score_risk(consistency)

    bill = (
        "National Grid\nAccount number 123\nStatement date 01/01/2020\n"
        "Previous balance $100.00\nAmount due $130.00\n"
        "[Company Logo]\nSample of\nLorem ipsum dolor sit amet\n"
    )
    tpl = find_template_leftovers(
        bill, scan_profile="forensic", source="Exhibit S_12", doc_kind="exhibit"
    )
    tpl_mat = [t for t in tpl if is_material_finding(t)]

    summary = {
        "tag": "restore-2026-09-22-detector",
        "sha_922": "3c411419d35ceec37852079d568701f497e1a150",
        "routing_rows": rows,
        "head_strictly_better_on_caption_scan": any(
            r["head_wins_scan"] for r in rows if r["case"] == "blank_caption_scan_heavy"
        ),
        "exhibit_both_forensic": any(
            r["both_forensic"] for r in rows if r["case"] == "exhibit_filename_scan"
        ),
        "wayne_head": {
            "occupancy_conflicts": len(occ),
            "occupancy_severity": (occ[0].get("severity") if occ else None),
            "pairwise_hits": len(consistency),
            "risk": risk,
            "score": score,
            "ok": len(occ) >= 1 and risk in {"yellow", "red"} and score >= 35,
        },
        "template_forensic_material_head": len(tpl_mat) >= 1,
        "all_routing_ok": all(r["ok"] for r in rows),
    }
    summary["all_pass"] = (
        summary["all_routing_ok"]
        and summary["head_strictly_better_on_caption_scan"]
        and summary["exhibit_both_forensic"]
        and summary["wayne_head"]["ok"]
        and summary["template_forensic_material_head"]
    )
    return summary


def main() -> None:
    result = evaluate()
    out = Path("/opt/cursor/artifacts/diff_922_vs_head.json")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    print(json.dumps(result, indent=2))
    if not result["all_pass"]:
        raise SystemExit(1)
    print("DIFF_922_OK")


if __name__ == "__main__":
    main()
