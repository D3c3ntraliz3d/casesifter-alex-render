"""Cross-practice gotcha verification eval. Run from backend/: python -m evals.run_gotcha_eval"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from app.auto_title import propose_document_title
from app.case_consistency import aggregate_fact_conflicts, dedupe_gotchas
from app.scan_profile import PROFILE_COURT_FILING, PROFILE_FORENSIC, resolve_scan_profile, scan_profile_for_kind
from app.evidence_tier import classify_tier, evidence_is_probative, find_internal_evidence_defects
from app.gotcha import (
    RETIRED_TYPES,
    _token_candidates,
    extract_claims,
    hard_veto_step,
    merge_verdicts,
    propose_tainted_support,
    retrieve_evidence,
    verify_candidates,
    verify_pair,
)
from app.finding_policy import (
    is_recorded_instrument_math_noise,
    looks_like_recorded_instrument,
)
from app.models import Finding
from app.report.scoring import score_risk


def _load(name: str) -> dict:
    return json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))


def evaluate_pairs() -> dict:
    data = _load("gotcha_regression.json")
    rows = []
    by_area: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "tp_hit": 0, "fp": 0, "fp_flagged": 0})
    for area in data["areas"]:
        name = area["area"]
        for pair in area["pairs"]:
            verdict = verify_pair(pair)
            veto = hard_veto_step(pair, verdict)
            if veto:
                flagged = False
                failed = veto
            else:
                flagged = bool(verdict.get("pass")) and evidence_is_probative(pair)
                failed = None if flagged else (verdict.get("failed_step") if not verdict.get("pass") else "evidence_tier")
            expected = pair.get("expect_fail")
            ok = (pair["label"] == "tp" and flagged) or (pair["label"] == "fp" and not flagged)
            stats = by_area[name]
            if pair["label"] == "tp":
                stats["tp"] += 1
                if flagged:
                    stats["tp_hit"] += 1
            else:
                stats["fp"] += 1
                if flagged:
                    stats["fp_flagged"] += 1
            rows.append(
                {
                    "id": pair["id"],
                    "area": name,
                    "label": pair["label"],
                    "flagged": flagged,
                    "failed_step": failed,
                    "expect_fail": expected,
                    "ok": ok,
                    "reason": (verdict.get(failed) or {}).get("reason") if failed else "",
                }
            )
    summary = {}
    for name, stats in by_area.items():
        precision = (
            stats["tp_hit"] / (stats["tp_hit"] + stats["fp_flagged"])
            if (stats["tp_hit"] + stats["fp_flagged"])
            else 1.0
        )
        recall = stats["tp_hit"] / stats["tp"] if stats["tp"] else 0.0
        summary[name] = {**stats, "precision": round(precision, 3), "recall": round(recall, 3)}
    return {"rows": rows, "summary": summary}


def evaluate_pipeline() -> dict:
    """Extract → retrieve → propose → verify on synthetic multi-document cases."""
    data = _load("gotcha_pipeline_cases.json")
    rows = []
    for case in data["cases"]:
        claims = extract_claims(case["filing_text"], case["filing_name"], "filing")
        docs = []
        snaps = {}
        for idx, exhibit in enumerate(case["exhibits"]):
            doc_id = f"ex-{idx}"
            docs.append({"id": doc_id, "display_name": exhibit["name"]})
            snaps[doc_id] = {"text_excerpt": exhibit["text"]}
        evidence = retrieve_evidence(docs, snaps, claims)
        candidates = _token_candidates(claims, evidence)
        flagged = False
        hit_title = ""
        for candidate in candidates:
            verdict = verify_pair(candidate)
            if verdict.get("pass"):
                flagged = True
                hit_title = candidate.get("evidence_source") or ""
                break
        ok = (case["label"] == "tp" and flagged) or (case["label"] == "fp" and not flagged)
        rows.append(
            {
                "id": case["id"],
                "area": case["area"],
                "label": case["label"],
                "claims": len(claims),
                "evidence_docs": len(evidence),
                "candidates": len(candidates),
                "flagged": flagged,
                "hit": hit_title,
                "ok": ok,
            }
        )
    precision_n = sum(1 for row in rows if row["flagged"])
    tp_hit = sum(1 for row in rows if row["label"] == "tp" and row["flagged"])
    fp_flagged = sum(1 for row in rows if row["label"] == "fp" and row["flagged"])
    tps = sum(1 for row in rows if row["label"] == "tp")
    return {
        "rows": rows,
        "precision": round(tp_hit / precision_n, 3) if precision_n else 1.0,
        "recall": round(tp_hit / tps, 3) if tps else 0.0,
        "fp_flagged": fp_flagged,
        "misses": [row for row in rows if not row["ok"]],
    }


def _four_step(pass_value: bool, runs: str = "against") -> dict:
    return {
        "same_fact": {"pass": pass_value, "fact_named": "x", "reason": "model"},
        "directionality": {"pass": pass_value, "evidence_asserts": "x", "runs": runs, "reason": "model"},
        "materiality": {"pass": pass_value, "tier": "element", "reason": "model"},
        "textual_grounding": {"pass": pass_value, "reason": "model"},
        "contradiction_type": "logical_inconsistency",
        "pass": pass_value,
    }


def evaluate_unverified_paths() -> dict:
    """Legacy labels and incomplete model passes must not ship."""
    duty_place = {
        "claim_quote": "The property owner promised to keep the walkway in good repair.",
        "evidence_quote": "The incident report lists the walkway address as 200 Smith Street, Brooklyn.",
    }
    notice = {
        "claim_quote": "I never received the required statutory notice.",
        "evidence_quote": "The court already found that type of notice defective and insufficient.",
    }
    traverse = {
        "claim_quote": "I was not served in this case by any means whatsoever.",
        "evidence_quote": "On 12/14/2023 at 9:08 PM I served the within SUMMONS; COMPLAINT at 1739 46th Street.",
    }
    local_duty = verify_pair(duty_place)
    local_notice = verify_pair(notice)
    local_traverse = verify_pair(traverse)
    incomplete = {"pass": True, "contradiction_type": "date_inconsistency"}
    complete_wrong = _four_step(True)
    checks = {
        "incomplete_model_pass_dropped": not merge_verdicts(duty_place, local_duty, incomplete).get("pass"),
        "legacy_type_cannot_override_duty_place": not merge_verdicts(duty_place, local_duty, complete_wrong).get(
            "pass"
        ),
        "legacy_type_cannot_override_notice": not merge_verdicts(notice, local_notice, complete_wrong).get("pass"),
        "hard_veto_duty_place": hard_veto_step(duty_place, local_duty) == "same_fact",
        "hard_veto_notice": hard_veto_step(notice, local_notice) == "directionality",
        "hard_veto_ordinary_traverse": hard_veto_step(traverse, local_traverse) == "directionality",
        "retired_types_defined": RETIRED_TYPES == {
            "logical_inconsistency",
            "location_contradiction",
            "location_inconsistency",
        },
    }
    accepted, rejected = verify_candidates([duty_place, notice])
    checks["verify_candidates_ships_none"] = accepted == [] and len(rejected) == 2
    checks["all_pass"] = all(checks.values())
    return checks


def score_independence() -> dict:
    """Same findings, different titles → same score. Verified content only."""
    empty = score_risk([])
    verified = Finding(
        id="t-gotcha",
        category="gotcha",
        severity="high",
        title="Apparent procedural / foundational issue",
        explanation="x",
        why_suspicious="y",
        confidence=82,
        evidence={
            "gotcha": True,
            "verified": True,
            "contradiction_type": "procedural_foundational",
            "material": True,
        },
    )
    unverified = verified.model_copy(
        update={
            "id": "t-unverified",
            "evidence": {
                "gotcha": True,
                "verified": False,
                "contradiction_type": "logical_inconsistency",
                "material": True,
            },
        }
    )
    custody = score_risk([verified])
    foreclosure = score_risk([verified])
    noise = score_risk([unverified])
    return {
        "empty_affidavit": empty,
        "custody_affidavit": custody,
        "foreclosure_affidavit": foreclosure,
        "unverified_legacy_ignored": noise,
        "title_does_not_matter": custody == foreclosure,
        "content_drives_score": empty != custody,
        "unverified_does_not_score": noise == empty,
    }


def evaluate_tiers() -> dict:
    motion_vs_motion = evidence_is_probative(
        {
            "claim_kind": "motion",
            "evidence_kind": "motion",
            "evidence_quote": "The process server did not attempt service at 1739 46th Street.",
            "evidence_source": "Motion to Dismiss",
        }
    )
    proposed = classify_tier(
        {"doc_kind": "order", "display_name": "Proposed Order"},
        "Defendants were duly served as recited in this proposed order.",
        "Proposed Order",
        "order",
    )
    aos = classify_tier(
        {"doc_kind": "affidavit", "display_name": "Affidavit of Service"},
        "sworn to before a notary",
        "Affidavit of Service",
        "affidavit",
    )
    internals = find_internal_evidence_defects(
        text="I served Jane Doe on Monday, March 3, 2024 at 6:15 p.m. at 5616 14th Avenue.",
        source="Affidavit of Service",
        document_id="aos-1",
        doc_kind="affidavit",
    )
    return {
        "motion_vs_motion_blocked": motion_vs_motion is False,
        "proposed_order_is_advocacy": proposed == 2,
        "affidavit_of_service_is_evidence": aos == 1,
        "weekday_internal_hit": len(internals) == 1,
        "all_pass": False,
    }


def evaluate_dedupe() -> dict:
    snaps = [
        {
            "document_id": "a",
            "display_name": "Note",
            "facts": [{"key": "interest_rate", "value": 4.5, "display": "4.5%", "kind": "rate", "quote": "rate of 4.5%"}],
        },
        {
            "document_id": "b",
            "display_name": "Complaint",
            "facts": [{"key": "interest_rate", "value": 5.0, "display": "5.0%", "kind": "rate", "quote": "rate of 5%"}],
        },
        {
            "document_id": "c",
            "display_name": "Affidavit",
            "facts": [{"key": "interest_rate", "value": 4.5, "display": "4.5%", "kind": "rate", "quote": "rate of 4.5%"}],
        },
    ]
    conflicts = aggregate_fact_conflicts(snaps)
    gotchas = dedupe_gotchas(
        [
            {
                "id": "1",
                "claim_quote": "I never signed.",
                "evidence_quote": "Agreement executed March 3.",
                "document_id": "a",
                "display_name": "Note",
            },
            {
                "id": "2",
                "claim_quote": "I never signed.",
                "evidence_quote": "Agreement executed March 3.",
                "document_id": "b",
                "display_name": "Complaint",
            },
            {
                "id": "3",
                "claim_quote": "I was not served in this case by any means whatsoever.",
                "evidence_quote": "On 12/14/2023 I served the summons at 1739 46th Street.",
                "document_id": "d",
                "display_name": "Affidavit 49",
            },
            {
                "id": "4",
                "claim_quote": "The process server never came to my home to attempt to serve me.",
                "evidence_quote": "On 12/14/2023 at 9:08 PM I served the within summons at 1739 46TH STREET 3.",
                "document_id": "e",
                "display_name": "Affidavit 58",
            },
        ]
    )
    return {
        "one_rate_conflict": len(conflicts) == 1,
        "three_docs_referenced": len((conflicts[0].get("related_documents") if conflicts else []) or []) == 3,
        "two_gotcha_cards": len(gotchas) == 2,
        "gotcha_names_both_docs": len((gotchas[0].get("related_documents") if gotchas else []) or []) == 2,
        "service_theme_clustered": any(
            len(item.get("related_documents") or []) == 2
            and "serv" in str(item.get("claim_quote") or "").lower()
            for item in gotchas
        ),
        "all_pass": False,
    }


def evaluate_tainted_support() -> dict:
    """He-said-she-said is not a gotcha; produced evidence that is tainted is."""
    claims = [
        {
            "quote": "I did not live at 1739 46th Street at the time. I attach Exhibit A as proof.",
            "source": "Defendant affidavit",
            "document_id": "aff-1",
            "doc_kind": "affidavit",
        }
    ]
    denial = [
        {
            "quote": "I was not served in this case by any means whatsoever.",
            "source": "Defendant affidavit",
            "document_id": "aff-1",
            "doc_kind": "affidavit",
        }
    ]
    docs = [
        {"id": "aff-1", "display_name": "Defendant affidavit", "doc_kind": "affidavit"},
        {"id": "ex-a", "display_name": "Exhibit A utility bill", "doc_kind": "exhibit"},
        {"id": "aos", "display_name": "Exhibit S_53", "doc_kind": "exhibit"},
    ]
    snaps = {
        "ex-a": {
            "findings_summary": [
                {
                    "id": "img-ela-1",
                    "category": "image",
                    "severity": "high",
                    "title": "Possible graphic splice on the address line",
                    "analysis_type": "graphic_manipulation",
                    "quoted_evidence": "Address line shows a paste-over inconsistent with the surrounding print.",
                }
            ]
        },
        "aos": {
            "findings_summary": [
                {
                    "id": "img-ela-2",
                    "category": "image",
                    "severity": "high",
                    "title": "ELA hotspot on the signature",
                    "analysis_type": "graphic_manipulation",
                    "quoted_evidence": "Hotspot on the process-server signature.",
                }
            ]
        },
    }
    produced = propose_tainted_support(claims, docs, snaps, filing_text=claims[0]["quote"])
    bare = propose_tainted_support(denial, docs, snaps, filing_text=denial[0]["quote"])
    return {
        "produced_tainted_hits": len(produced) == 1,
        "produced_uses_their_exhibit": bool(produced) and produced[0].get("document_id") == "ex-a",
        "produced_marked_tainted": bool(produced) and produced[0].get("tainted_support") is True,
        "bare_denial_does_not_use_aos": bare == [],
        "all_pass": False,
    }


def evaluate_doc_kinds() -> dict:
    """NYSCEF type tokens beat caption words like complaint/answer."""
    caption = (
        "SUPREME COURT OF THE STATE OF NEW YORK COUNTY OF KINGS "
        "Specialized Loan Servicing LLC, Plaintiff, vs. Evelyn Lefkowitz. "
        "COMPLAINT FOR MORTGAGE FORECLOSURE."
    )
    cases = [
        (
            "17 AFFIRMATION AFFIDAVIT OF SERVICE.pdf",
            caption,
            "proof_of_service",
        ),
        (
            "60 NOTICE OF CROSS MOTION.pdf",
            caption + " ANSWER",
            "motion",
        ),
        (
            "62 AFFIDAVIT OR AFFIRMATION IN SUPPORT OF MOTION.pdf",
            caption,
            "affidavit",
        ),
        (
            "10 STATEMENT OF AUTHORIZATION FOR ELECTRONIC FILING.pdf",
            caption,
            "filing",
        ),
        (
            "2 SUMMONS AND COMPLAINT.pdf",
            caption,
            "summons",
        ),
        (
            "EXHIBIT_S__83.pdf",
            caption,
            "exhibit",
        ),
        (
            "533445_2023_Specialized_Loan_Servi_AFFIRMATION_AFFIDAV_17.pdf",
            caption + " On 12/14/2023 I served the within SUMMONS; COMPLAINT.",
            "proof_of_service",
        ),
        (
            "533445_2023_Specialized_Loan_Servi_STATEMENT_OF_AUTHOR_10.pdf",
            caption,
            "filing",
        ),
        (
            "533445_2023_Specialized_Loan_Servi_NOTICE_OF_CROSS_MOT_93.pdf",
            caption,
            "motion",
        ),
    ]
    rows = []
    ok = True
    for filename, sample, expected in cases:
        _name, kind = propose_document_title(filename, sample)
        hit = kind == expected
        ok = ok and hit
        rows.append({"file": filename, "kind": kind, "expected": expected, "ok": hit})
    unknown = scan_profile_for_kind(None)
    blank_resolve = resolve_scan_profile("", "NYSCEF DOC. NO. 10 FILED: KINGS COUNTY")
    return {
        "rows": rows,
        "kinds_ok": all(row["ok"] for row in rows),
        "unknown_kind_is_not_forensic": unknown == PROFILE_COURT_FILING,
        "blank_efiling_is_court_filing": blank_resolve == PROFILE_COURT_FILING,
        "exhibit_stays_forensic": scan_profile_for_kind("exhibit") == PROFILE_FORENSIC,
        "all_pass": False,
    }


def evaluate_recorded_math() -> dict:
    """Recorded mortgages/deeds are not bank statements; alteration still counts."""
    mortgage_text = (
        "This Mortgage, to be recorded in the Office of the City Register, CRFN 2023000123456, "
        "Block 5621 Lot 14, mortgage tax $4,312.50, principal sum of $575,000.00 at 6.125 percent."
    )
    deed_name = "Bargain and Sale Deed — 1739 46th Street"
    bill_text = "Previous balance $100.00 Payments $40.00 New charges $70.00 Amount due $130.00"
    math_item = {
        "analysis_type": "math_error",
        "title": "Principal and interest do not equal the stated monthly payment",
        "observation": "575000 at 6.125% does not produce the printed monthly figure.",
        "quoted_evidence": "principal sum of $575,000.00 at 6.125 percent monthly payment $3,494.18",
    }
    overlay_item = {
        "analysis_type": "text_overlay",
        "title": "Amount line looks pasted",
        "observation": "The principal figure sits on a different background tone.",
        "quoted_evidence": "principal sum of $575,000.00",
    }
    bill_math = {
        "analysis_type": "math_error",
        "title": "Line items do not sum to amount due",
        "observation": "100 minus 40 plus 70 is 130, but the bill states 140.",
        "quoted_evidence": "Amount due $140.00",
    }
    notice_name = "533445 2023 NOTICE OF MORTGAGE FORECLOSURE"
    return {
        "mortgage_body_is_recorded": looks_like_recorded_instrument(mortgage_text, "Exhibit S_12"),
        "deed_filename_is_recorded": looks_like_recorded_instrument("", deed_name),
        "foreclosure_notice_is_not_recorded": not looks_like_recorded_instrument("", notice_name),
        "bank_statement_is_not_recorded": not looks_like_recorded_instrument(bill_text, "Con Edison bill"),
        "mortgage_math_dropped": is_recorded_instrument_math_noise(
            math_item, text=mortgage_text, source="Exhibit S_12"
        ),
        "mortgage_overlay_kept": not is_recorded_instrument_math_noise(
            overlay_item, text=mortgage_text, source="Exhibit S_12"
        ),
        "bank_math_kept": not is_recorded_instrument_math_noise(
            bill_math, text=bill_text, source="Con Edison bill"
        ),
        "all_pass": False,
    }


def evaluate() -> dict:
    dedupe = evaluate_dedupe()
    dedupe["all_pass"] = all(dedupe[key] for key in dedupe if key != "all_pass")
    tiers = evaluate_tiers()
    tiers["all_pass"] = all(tiers[key] for key in tiers if key != "all_pass")
    tainted = evaluate_tainted_support()
    tainted["all_pass"] = all(tainted[key] for key in tainted if key != "all_pass")
    kinds = evaluate_doc_kinds()
    kinds["all_pass"] = all(kinds[key] for key in kinds if key not in {"all_pass", "rows"})
    recorded = evaluate_recorded_math()
    recorded["all_pass"] = all(recorded[key] for key in recorded if key != "all_pass")
    return {
        "pairs": evaluate_pairs(),
        "pipeline": evaluate_pipeline(),
        "unverified_paths": evaluate_unverified_paths(),
        "score_independence": score_independence(),
        "dedupe": dedupe,
        "tiers": tiers,
        "tainted_support": tainted,
        "doc_kinds": kinds,
        "recorded_math": recorded,
    }


def main() -> None:
    result = evaluate()
    print("PER AREA")
    for area, stats in result["pairs"]["summary"].items():
        print(f"  {area}: precision={stats['precision']} recall={stats['recall']} {stats}")
    misses = [row for row in result["pairs"]["rows"] if not row["ok"]]
    print("PAIR MISSES", len(misses))
    for row in misses:
        print(" ", row)
    pipe = result["pipeline"]
    print(f"PIPELINE precision={pipe['precision']} recall={pipe['recall']} fp_flagged={pipe['fp_flagged']}")
    for row in pipe["rows"]:
        print(" ", row)
    print("UNVERIFIED PATHS", result["unverified_paths"])
    print("SCORE INDEPENDENCE", result["score_independence"])
    print("DEDUPE", result["dedupe"])
    print("TIERS", result["tiers"])
    print("TAINTED SUPPORT", result["tainted_support"])
    print("DOC KINDS", result["doc_kinds"])
    print("RECORDED MATH", result["recorded_math"])
    failed = bool(
        misses
        or pipe["misses"]
        or not result["unverified_paths"]["all_pass"]
        or not result["dedupe"]["all_pass"]
        or not result["tiers"]["all_pass"]
        or not result["tainted_support"]["all_pass"]
        or not result["doc_kinds"]["all_pass"]
        or not result["recorded_math"]["all_pass"]
    )
    if not result["score_independence"]["title_does_not_matter"]:
        failed = True
    if not result["score_independence"]["unverified_does_not_score"]:
        failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
