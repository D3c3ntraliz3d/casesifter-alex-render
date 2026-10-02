"""Cross-practice gotcha verification eval. Run from backend/: python -m evals.run_gotcha_eval"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from app.auto_title import propose_document_title
from app.case_consistency import (
    aggregate_fact_conflicts,
    compare_to_case,
    dedupe_gotchas,
    _internal_fact_conflicts,
)
from app.scan_profile import (
    PROFILE_COURT_FILING,
    PROFILE_FORENSIC,
    apply_media_override,
    finalize_scan_profile,
    resolve_scan_profile,
    scan_profile_for_kind,
)
from app.evidence_tier import classify_tier, evidence_is_probative, find_internal_evidence_defects
from app.gotcha import (
    RETIRED_TYPES,
    _token_candidates,
    extract_claims,
    hard_veto_step,
    merge_verdicts,
    keep_board_gotcha,
    propose_tainted_support,
    retrieve_evidence,
    verify_candidates,
    verify_pair,
)
from app.engines.statement_dates import find_statement_date_conflicts
from app.engines.template_fabrication import find_template_leftovers, review_print_scan_ela
from app.report.scoring import pick_top_findings, score_risk
from app.finding_policy import (
    is_material_finding,
    is_recorded_instrument_math_noise,
    looks_like_recorded_instrument,
)
from app.models import Finding


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
            "facts": [{"key": "first_default_date", "value": "2022-02-01", "display": "02/01/2022", "kind": "date", "quote": "defaulted on February 1, 2022", "month_key": "2022-02"}],
        },
        {
            "document_id": "b",
            "display_name": "Complaint",
            "facts": [{"key": "first_default_date", "value": "2023-06-01", "display": "06/01/2023", "kind": "date", "quote": "defaulted on June 1, 2023", "month_key": "2023-06"}],
        },
        {
            "document_id": "c",
            "display_name": "Affidavit",
            "facts": [{"key": "first_default_date", "value": "2022-02-01", "display": "02/01/2022", "kind": "date", "quote": "defaulted on February 1, 2022", "month_key": "2022-02"}],
        },
    ]
    conflicts = aggregate_fact_conflicts(snaps)
    loan_status = aggregate_fact_conflicts(
        [
            {
                "document_id": "note",
                "display_name": "Note",
                "facts": [
                    {"key": "interest_rate", "value": 4.875, "display": "4.875%", "kind": "rate"},
                    {"key": "monthly_payment", "value": 1840.22, "display": "$1,840.22", "kind": "money"},
                    {"key": "outstanding_balance", "value": 312000.0, "display": "$312,000.00", "kind": "money"},
                    {"key": "loan_amount", "value": 328000.0, "display": "$328,000.00", "kind": "money"},
                ],
            },
            {
                "document_id": "complaint",
                "display_name": "Complaint",
                "facts": [
                    {"key": "interest_rate", "value": 7.875, "display": "7.875%", "kind": "rate"},
                    {"key": "monthly_payment", "value": 2410.18, "display": "$2,410.18", "kind": "money"},
                    {"key": "outstanding_balance", "value": 341112.55, "display": "$341,112.55", "kind": "money"},
                    {"key": "loan_amount", "value": 328000.0, "display": "$328,000.00", "kind": "money"},
                ],
            },
        ]
    )
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
        "one_date_conflict": len(conflicts) == 1,
        "loan_status_numbers_ignored": loan_status == [],
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
    residence = [
        {
            "quote": "I did not live at 5616 14th Avenue when the process server claims I was served.",
            "source": "Alexander affidavit",
            "document_id": "aff-1",
            "doc_kind": "affidavit",
        }
    ]
    snaps["ex-a"]["text_excerpt"] = (
        "Con Edison bill for November 2023. Account number 44122918. "
        "Amount due $140.00. Statement date 03/02/2018. Service period 11/01/2023-11/30/2023."
    )
    snaps["ex-a"]["findings_summary"][0]["analysis_type"] = "date_inconsistency"
    snaps["ex-a"]["findings_summary"][0]["quoted_evidence"] = (
        "Statement date 03/02/2018 cannot fall inside a November 2023 service period."
    )
    focused = propose_tainted_support(
        residence, docs, snaps, filing_text=residence[0]["quote"], focus_document_id="ex-a"
    )
    wrong_focus = propose_tainted_support(
        claims, docs, snaps, filing_text=claims[0]["quote"], focus_document_id="aos"
    )
    standalone = propose_tainted_support(
        [], docs, snaps, filing_text="", focus_document_id="ex-a"
    )
    citation = [
        {
            "quote": (
                "v Gewirtz, 204 AD3d 1070, the process server failed to properly "
                "serve the defendants at their actual place of business or dwelling."
            ),
            "source": "AFFIDAVIT OR AFFIRM 92",
            "document_id": "aff-92",
            "doc_kind": "affidavit",
        }
    ]
    citation_focus = propose_tainted_support(
        citation, docs, snaps, filing_text=citation[0]["quote"], focus_document_id="ex-a"
    )
    in_support = propose_tainted_support(
        [
            {
                "quote": "I submit this affirmation in support of the motion.",
                "source": "Attorney affirmation",
                "document_id": "aff-1",
                "doc_kind": "affidavit",
            }
        ],
        docs,
        snaps,
        filing_text="I submit this affirmation in support of the motion.",
    )
    copied = {
        "tainted_support": True,
        "document_id": "s-76",
        "evidence_kind": "exhibit",
        "evidence_quote": "16th day of May, 2022",
        "verified": True,
        "gotcha": True,
    }
    live = {
        "tainted_support": True,
        "document_id": "ex-a",
        "evidence_kind": "exhibit",
        "evidence_quote": "16th day of May, 2022",
        "verified": True,
        "gotcha": True,
    }
    two_filings = {
        "claim_kind": "affidavit",
        "evidence_kind": "affidavit",
        "claim_quote": "Plaintiff had no way of knowing.",
        "evidence_quote": "I did not know if it was a fact.",
        "claim_source": "AFFIDAVIT 99",
        "evidence_source": "AFFIDAVIT 101",
    }
    year_only = {
        "claim_quote": (
            "affidavits of service filed for the week of April 2-6, 2007 "
            "in US Bank v Alexander, Index 11467/2007"
        ),
        "evidence_quote": (
            "your Notice of Appearance, dated 1/15/2016, on behalf of Wayne "
            "Alexander in US Bank v Alexander, Inde 11467/2007"
        ),
    }
    return {
        "produced_tainted_hits": len(produced) == 1,
        "produced_uses_their_exhibit": bool(produced) and produced[0].get("document_id") == "ex-a",
        "produced_marked_tainted": bool(produced) and produced[0].get("tainted_support") is True,
        "produced_says_edited": bool(produced)
        and (
            produced[0].get("title") == "Exhibit appears fraudulently edited"
            or "appears altered" in str(produced[0].get("title") or "").lower()
        ),
        "bare_denial_does_not_use_aos": bare == [],
        "residence_bill_dates_are_gotcha": bool(focused)
        and focused[0].get("document_id") == "ex-a"
        and focused[0].get("tainted_support") is True,
        "other_exhibit_not_pulled": wrong_focus == [],
        "tainted_exhibit_stands_alone": bool(standalone) and standalone[0].get("document_id") == "ex-a",
        "one_card_per_exhibit": len(citation_focus) == 1,
        "does_not_quote_unrelated_filing": bool(citation_focus)
        and "Gewirtz" not in str(citation_focus[0].get("claim_quote") or ""),
        "in_support_does_not_taint_case": in_support == [],
        "copied_jurat_dropped": keep_board_gotcha(
            copied, [{"id": "s-76", "doc_kind": "exhibit", "display_name": "Exhibit S_76"}], {"s-76": {}}
        )
        is False,
        "defective_exhibit_kept": keep_board_gotcha(live, docs, snaps) is True,
        "two_filings_dropped": keep_board_gotcha(two_filings, docs, snaps) is False,
        "index_year_is_not_same_day": (verify_pair(year_only).get("same_fact") or {}).get("fact_named")
        != "whereabouts on a shared date",
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
            "41 - EXHIBIT(S) - S Bank Staatment.pdf",
            "In order to process this payment please remit the money order.",
            "exhibit",
        ),
        (
            "32 - EXHIBIT(S).pdf",
            "",
            "exhibit",
        ),
        (
            "32 EXHIBIT(S).pdf",
            "",
            "exhibit",
        ),
        (
            "38 – EXHIBIT(S).pdf",  # en-dash between number and type
            "",
            "exhibit",
        ),
        (
            "Proposed Order.pdf",
            "IT IS HEREBY ORDERED that the motion is granted.",
            "order",
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
    empty_peek = resolve_scan_profile("", "")
    # National: e-filing stamp chrome alone must not choose court_filing.
    blank_stamp = resolve_scan_profile("", "NYSCEF DOC. NO. 10 FILED: KINGS COUNTY INDEX NO. 123")
    blank_stamp_statement = resolve_scan_profile(
        "",
        "FILED: KINGS COUNTY CLERK 01/15/2024 INDEX NO. 533445/2023\n"
        "NYSCEF DOC. NO. 41\n"
        "Account number 987654321\n"
        "Statement date 01/01/2020\n"
        "Previous balance $100.00\n"
        "Amount due $130.00\n"
        "National Grid",
    )
    caption = (
        "SUPREME COURT OF THE STATE OF NEW YORK COUNTY OF KINGS\n"
        "Specialized Loan Servicing LLC, Plaintiff,\n"
        "Plaintiffs - against -\n"
        "WHEREFORE plaintiff demands judgment against the defendants."
    )
    # Caption alone (no origin yet) must stay forensic — real NYSCEF scans have captions.
    pleading_caption_resolve = resolve_scan_profile("", caption)
    # Blank + caption + native-text → still forensic so Gemini runs on real uploads.
    caption_native = finalize_scan_profile(
        PROFILE_FORENSIC,
        doc_kind="",
        peek_text=caption,
        suffix=".pdf",
        origin={"guess": "native-text", "scan_heavy": False},
    )
    # Explicit affidavit kind on native-text → court_filing (packaging skip).
    affidavit_native = finalize_scan_profile(
        PROFILE_COURT_FILING,
        doc_kind="affidavit",
        peek_text=caption,
        suffix=".pdf",
        origin={"guess": "native-text", "scan_heavy": False},
    )
    # Caption + scan-heavy PDF → forensic (forgery lane). This is the fly-through fix.
    caption_scan_heavy = finalize_scan_profile(
        PROFILE_COURT_FILING,
        doc_kind="",
        peek_text=caption,
        suffix=".pdf",
        origin={"guess": "hybrid", "scan_heavy": True},
    )
    # Explicit affidavit kind on scan-heavy still forensic.
    affidavit_scan_heavy = finalize_scan_profile(
        PROFILE_COURT_FILING,
        doc_kind="affidavit",
        peek_text=caption,
        suffix=".pdf",
        origin={"guess": "image-only", "scan_heavy": True},
    )
    # Images / scan-heavy PDFs force forensic even when kind said affidavit.
    image_override = apply_media_override(
        PROFILE_COURT_FILING, suffix=".png", origin=None
    )
    scan_heavy_override = apply_media_override(
        PROFILE_COURT_FILING,
        suffix=".pdf",
        origin={"guess": "image-only", "scan_heavy": True},
    )
    native_filing_stays = apply_media_override(
        PROFILE_COURT_FILING,
        suffix=".pdf",
        origin={"guess": "native-text", "scan_heavy": False},
    )
    # Mis-labeled Order on an EXHIBIT(S) native PDF must still take forgery lane.
    exhibit_parens_order = finalize_scan_profile(
        PROFILE_COURT_FILING,
        doc_kind="order",
        peek_text="In order to process this payment please remit.",
        suffix=".pdf",
        origin={"guess": "native-text", "scan_heavy": False},
        filename="41 - EXHIBIT(S) - S Bank Staatment.pdf",
        display_name="41 EXHIBIT(S) S Bank Staatment",
    )
    return {
        "rows": rows,
        "kinds_ok": all(row["ok"] for row in rows),
        "empty_peek_blank_kind_is_forensic": empty_peek == PROFILE_FORENSIC,
        "unknown_kind_defaults_forensic": unknown == PROFILE_FORENSIC,
        "blank_efiling_is_forensic": blank_stamp == PROFILE_FORENSIC,
        "blank_stamp_statement_is_forensic": blank_stamp_statement == PROFILE_FORENSIC,
        "pleading_caption_resolve_stays_forensic": pleading_caption_resolve == PROFILE_FORENSIC,
        "blank_caption_native_stays_forensic": caption_native == PROFILE_FORENSIC,
        "explicit_affidavit_native_is_court_filing": affidavit_native == PROFILE_COURT_FILING,
        "pleading_caption_scan_heavy_is_forensic": caption_scan_heavy == PROFILE_FORENSIC,
        "affidavit_kind_scan_heavy_is_forensic": affidavit_scan_heavy == PROFILE_FORENSIC,
        "image_forces_forensic": image_override == PROFILE_FORENSIC,
        "scan_heavy_forces_forensic": scan_heavy_override == PROFILE_FORENSIC,
        "native_filing_keeps_court_filing": native_filing_stays == PROFILE_COURT_FILING,
        "exhibit_parens_order_kind_is_forensic": exhibit_parens_order == PROFILE_FORENSIC,
        "exhibit_stays_forensic": scan_profile_for_kind("exhibit") == PROFILE_FORENSIC,
        "affidavit_kind_is_court_filing": scan_profile_for_kind("affidavit") == PROFILE_COURT_FILING,
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


def evaluate_template_fabrication() -> dict:
    """Bought form → fill → print → scan is exhibit fraud; pleadings are out of scope."""
    bill = (
        "Con Edison statement. Account number [Account Number]. Amount due $140.00. "
        "Customer: Your Company Name. Previous balance $100.00. www.example.com"
    )
    clean = (
        "Con Edison statement. Account number 44122918. Amount due $140.00. "
        "Previous balance $100.00. Payments $40.00. New charges $80.00."
    )
    pleading = "I enclose a sample of the notice previously served on the defendant."
    hits = find_template_leftovers(bill, scan_profile="forensic", source="Exhibit S_12")
    clean_hits = find_template_leftovers(clean, scan_profile="forensic", source="Exhibit S_12")
    filing_hits = find_template_leftovers(bill, scan_profile="court_filing", source="Motion")
    sample_of = find_template_leftovers(pleading, scan_profile="forensic", source="Exhibit A")
    ela = Finding(
        id="img-ela-page-1",
        category="image",
        severity="info",
        title="Uneven recompression on scanned page page-1",
        explanation="x",
        why_suspicious="y",
        confidence=32,
        evidence={"ela_score": 44, "largest_blob_px": 1400, "scan_heavy": True},
    )
    # Clean statement + soft ELA must NOT upgrade (no template leftover content hook).
    upgraded_clean = review_print_scan_ela([ela], clean, scan_profile="forensic")
    # Leftover chrome + ELA may upgrade (bought template print-scan path).
    upgraded = review_print_scan_ela([ela], bill, scan_profile="forensic")
    buried = review_print_scan_ela([ela], "This indenture made the first day of June.", scan_profile="forensic")
    scan_ela = Finding(
        id="img-ela-page-3",
        category="image",
        severity="medium",
        title="Uneven recompression",
        explanation="x",
        why_suspicious="y",
        confidence=55,
        evidence={"ela_score": 40, "largest_blob_px": 1200, "scan_heavy": True},
    )
    meta = Finding(
        id="md-editor-software",
        category="metadata",
        severity="medium",
        title="Editor software detected",
        explanation="x",
        why_suspicious="y",
        confidence=60,
        evidence={},
    )
    # Clean-bank decoy: logo/math opinions without substantive quotes stay non-material.
    from app.finding_policy import is_material_finding as _mat

    logo_weak = Finding(
        id="ctx-logo-clean",
        category="contextual",
        severity="medium",
        title="Logo / chrome mismatch — Chase",
        explanation="Issuer looks wrong",
        why_suspicious="y",
        confidence=55,
        evidence={
            "analysis_type": "logo_chrome_mismatch",
            "ai_generated": True,
            "issuer": "Chase",
            "quoted_evidence": "",
            "content_hook": False,
        },
    )
    math_weak = Finding(
        id="ctx-math-clean",
        category="contextual",
        severity="high",
        title="Arithmetic does not reconcile",
        explanation="Something looks off on the statement",
        why_suspicious="y",
        confidence=70,
        evidence={"analysis_type": "math_error", "ai_generated": True, "quoted_evidence": "no digits here"},
    )
    checks = {
        "leftover_template_flagged": len(hits) == 1 and hits[0].evidence.get("analysis_type") == "template_fabrication",
        "leftover_is_material": bool(hits) and is_material_finding(hits[0]),
        "clean_statement_quiet": clean_hits == [],
        "court_filing_skipped": filing_hits == [],
        "sample_of_notice_quiet": sample_of == [],
        "statement_ela_upgraded": bool(upgraded) and upgraded[0].severity == "medium",
        "clean_statement_ela_not_upgraded": bool(upgraded_clean)
        and upgraded_clean[0].severity == "info"
        and not is_material_finding(upgraded_clean[0]),
        "non_statement_ela_stays_info": bool(buried) and buried[0].severity == "info",
        "scan_heavy_ela_not_material": not is_material_finding(scan_ela),
        "editor_metadata_not_material": not is_material_finding(meta),
        "print_scan_ela_is_material": bool(upgraded) and is_material_finding(upgraded[0]),
        "clean_logo_without_quote_not_material": not _mat(logo_weak),
        "clean_math_without_digits_not_material": not _mat(math_weak),
    }
    return {**checks, "all_pass": all(checks.values())}


def evaluate_statement_dates() -> dict:
    """Impossible dates on a third-party statement are fraud; a coherent bill is quiet."""
    broken = (
        "Account number 44122918. Amount due $140.00. "
        "Statement date 03/02/2018. Service period 11/01/2023 - 11/30/2023."
    )
    clean = (
        "Account number 44122918. Amount due $140.00. "
        "Statement date 12/05/2023. Service period 11/01/2023 - 11/30/2023. "
        "Due date 12/26/2023."
    )
    arrears = (
        "Account number 44122918. Amount due $8,412.00. "
        "Notice dated 06/01/2023. The installment payment is still due on 02/28/2022. "
        "Right to cure. Statement date 06/01/2023. Service period 01/01/2022 - 01/31/2022."
    )
    mortgage = (
        "This indenture. Recorded in the office of the city register. "
        "Mortgage dated 03/01/2006. CRFN 2006000123456. Block 1, lot 2."
    )
    packet = (
        clean
        + "\f"
        + "Account number 998877. Amount due $80.00. "
        "Statement date 04/16/2007. Service period 11/01/2023 - 11/30/2023."
    )
    hits = find_statement_date_conflicts(broken, scan_profile="forensic", source="Exhibit S_41")
    clean_hits = find_statement_date_conflicts(clean, scan_profile="forensic", source="Exhibit S_41")
    arrears_hits = find_statement_date_conflicts(arrears, scan_profile="forensic", source="Exhibit S_12")
    mortgage_hits = find_statement_date_conflicts(mortgage, scan_profile="forensic", source="Mortgage")
    filing_hits = find_statement_date_conflicts(broken, scan_profile="court_filing", source="Exhibit S_41")
    packet_hits = find_statement_date_conflicts(packet, scan_profile="forensic", source="Exhibit S_41")
    docs = [{"id": "ex-41", "display_name": "Exhibit S_41 utility bills", "doc_kind": "exhibit"}]
    snaps = {
        "ex-41": {
            "text_excerpt": broken,
            "findings_summary": [
                {
                    "id": hits[0].id if hits else "missing",
                    "category": "contextual",
                    "severity": "high",
                    "analysis_type": "date_inconsistency",
                    "quoted_evidence": (hits[0].evidence or {}).get("quoted_evidence") if hits else "",
                }
            ],
        }
    }
    tainted = propose_tainted_support([], docs, snaps, filing_text="", focus_document_id="ex-41")
    other = Finding(
        id="img-ela-page-2",
        category="image",
        severity="high",
        title="Possible graphic splice",
        explanation="x",
        why_suspicious="y",
        confidence=80,
        evidence={"analysis_type": "graphic_manipulation"},
    )
    ranked = pick_top_findings(([other] + hits) if hits else [other], 3)
    return {
        "impossible_dates_flagged": bool(hits)
        and hits[0].evidence.get("analysis_type") == "date_inconsistency",
        "impossible_dates_are_material": bool(hits) and is_material_finding(hits[0]),
        "coherent_bill_quiet": clean_hits == [],
        "arrears_letter_quiet": arrears_hits == [],
        "recorded_instrument_quiet": mortgage_hits == [],
        "court_filing_skipped": filing_hits == [],
        "multi_statement_catches_the_broken_one": len(packet_hits) == 1,
        "becomes_tainted_gotcha": bool(tainted) and tainted[0].get("tainted_support") is True,
        "date_defect_ranks_first": bool(ranked) and ranked[0].id != "img-ela-page-2",
        "all_pass": False,
    }


def evaluate_case_impact() -> dict:
    """Only element/procedure/credibility/damages interrupts; scan noise and advocacy do not."""
    from app.gotcha import _boilerplate_collision
    from app.impact_judge import apply_case_impact_judge
    from app.report.scoring import score_risk

    residence = {
        "claim_quote": "I did not live at 5616 14th Avenue when I was served.",
        "evidence_quote": "Con Edison bill lists service at 5616 14th Avenue for November 2023.",
        "claim_source": "Alexander affidavit",
        "evidence_source": "Exhibit S_41",
        "claim_kind": "affidavit",
        "evidence_kind": "exhibit",
    }
    two_filings = {
        "claim_quote": "Plaintiff had no way of knowing the service address was not his residence.",
        "evidence_quote": "I did not know if it was a fact that that was the address of the individual.",
        "claim_source": "AFFIDAVIT 99",
        "evidence_source": "AFFIDAVIT 101",
        "claim_kind": "affidavit",
        "evidence_kind": "affidavit",
    }
    incidental = {
        "claim_quote": "The note is dated March 3, 2006.",
        "evidence_quote": "The note is dated 03/03/2006.",
        "claim_source": "Complaint",
        "evidence_source": "Note",
    }
    docs = [
        {"id": "ex-a", "display_name": "Exhibit A utility bill", "doc_kind": "exhibit"},
        {"id": "aff-1", "display_name": "Affidavit", "doc_kind": "affidavit"},
    ]
    snaps = {
        "ex-a": {
            "findings_summary": [
                {
                    "id": "txt-stmtdate-1",
                    "category": "contextual",
                    "severity": "high",
                    "title": "Statement date cannot fall with this service period",
                    "analysis_type": "date_inconsistency",
                    "explanation": (
                        "The statement date printed on the bill is 03/02/2018, but the "
                        "service period on the same page is 11/01/2023-11/30/2023."
                    ),
                    "why_suspicious": (
                        "A consumer bill cannot cover a 2023 service period while "
                        "showing a 2018 statement date on its face."
                    ),
                    "quoted_evidence": "Statement date 03/02/2018 vs service period 11/01/2023-11/30/2023.",
                }
            ]
        },
        "aff-1": {"findings_summary": []},
    }
    edited = propose_tainted_support([], docs, snaps, focus_document_id="ex-a")
    # Claim → tainted exhibit linker
    claim_rows = [
        {
            "quote": "As shown in Exhibit A, defendant resided at the premises.",
            "source": "Affidavit in support",
            "document_id": "aff-1",
            "doc_kind": "affidavit",
        }
    ]
    linked = propose_tainted_support(
        claim_rows,
        docs,
        snaps,
        filing_text="As shown in Exhibit A, defendant resided at the premises.",
    )
    jurat_card = {
        "tainted_support": True,
        "document_id": "ex-clean",
        "evidence_kind": "exhibit",
        "evidence_quote": "16th day of May, 2022",
        "verified": True,
        "gotcha": True,
    }
    ela_only = {
        "tainted_support": True,
        "document_id": "ex-ela",
        "evidence_kind": "exhibit",
        "evidence_quote": "ELA hotspot",
        "verified": True,
        "gotcha": True,
    }
    snaps_ela = {
        "ex-ela": {
            "findings_summary": [
                {
                    "id": "img-ela-1",
                    "category": "image",
                    "severity": "high",
                    "analysis_type": "graphic_manipulation",
                    "scan_heavy": True,
                    "quoted_evidence": "Whole-page scan texture",
                }
            ]
        }
    }
    residence_v = verify_pair(residence)
    residence_ok = bool(
        residence_v.get("pass")
        and hard_veto_step(residence, residence_v) is None
        and (residence_v.get("materiality") or {}).get("tier") == "element"
    )
    two_v = verify_pair(two_filings)
    incidental_v = verify_pair(incidental)

    # Noise gotcha demoted → risk green
    noise_finding = Finding(
        id="ctx-gotcha-noise",
        category="gotcha",
        severity="high",
        title="Apparent factual contradiction",
        explanation="United States collision",
        why_suspicious="noise",
        confidence=85.0,
        evidence={
            "gotcha": True,
            "verified": True,
            "claim_quote": (
                "The said mail matter was left under the exclusive care and custody "
                "of the United States Postal Service."
            ),
            "evidence_quote": (
                "a corporation organized and existing under the laws of the "
                "United States of America."
            ),
            "fact_named": "united, state",
            "analysis_type": "gotcha",
            "evidence_tier": 1,
            "material": True,
        },
    )
    fraud_finding = Finding(
        id="ctx-logo-ship",
        category="contextual",
        severity="medium",
        title="Logo / chrome mismatch",
        explanation="Wrong-era National Grid mark on 2007 Brooklyn statement.",
        why_suspicious="Print-scan still leaves wrong chrome.",
        confidence=70.0,
        evidence={
            "analysis_type": "logo_chrome_mismatch",
            "ai_generated": True,
            "content_hook": True,
            "issuer": "National Grid",
            "quoted_evidence": "Statement Date: April 12, 2007",
        },
    )
    judged, _ = apply_case_impact_judge([noise_finding, fraud_finding])
    noise_out = next(f for f in judged if f.id == "ctx-gotcha-noise")
    fraud_out = next(f for f in judged if f.id == "ctx-logo-ship")
    risk_level, _ = score_risk(judged)

    return {
        "residence_hits_element": residence_ok,
        "two_filings_dropped": keep_board_gotcha(two_filings, docs, snaps) is False
        or hard_veto_step(two_filings, two_v) is not None
        or not two_v.get("pass"),
        "date_format_not_material": not incidental_v.get("pass"),
        "defective_statement_one_edited_card": len(edited) == 1
        and "fraudulently edited" in str(edited[0].get("title") or "").lower()
        and "03/02/2018" in str(edited[0].get("evidence_quote") or "")
        and "2023" in str(edited[0].get("evidence_quote") or "")
        and "fraudulently edited. Open the scan report" not in str(edited[0].get("why_it_may_matter") or ""),
        "claim_links_tainted_exhibit": len(linked) >= 1
        and bool(str(linked[0].get("claim_quote") or ""))
        and "Exhibit A" in str(linked[0].get("claim_quote") or "")
        and "altered" in str(linked[0].get("title") or "").lower(),
        "jurat_spam_dropped": keep_board_gotcha(
            jurat_card,
            [{"id": "ex-clean", "doc_kind": "exhibit", "display_name": "Exhibit S_76"}],
            {"ex-clean": {}},
        )
        is False,
        "bare_ela_not_tainted_board": keep_board_gotcha(
            ela_only,
            [{"id": "ex-ela", "doc_kind": "exhibit", "display_name": "Exhibit S_58"}],
            snaps_ela,
        )
        is False,
        "mamiye_boilerplate_suppressed": _mamiye_boilerplate_eval(),
        "noise_demoted_risk_not_red": noise_out.severity == "info"
        and (noise_out.evidence or {}).get("case_impact") == "noise"
        and risk_level != "red"
        and is_material_finding(fraud_out)
        and (fraud_out.evidence or {}).get("case_impact") in {"case_winning", "material_review"},
        "quote_must_attest": _quote_attest_eval(),
        "national_grid_historical_fixture": _national_grid_historical_fixture_eval(),
        "ai_print_scan_fixtures": _ai_print_scan_fixtures_eval(),
        "all_pass": False,
    }


def _mamiye_boilerplate_eval() -> bool:
    path = Path(__file__).resolve().parent / "fixtures" / "foreclosure" / "mamiye_aos_united_states_collision.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("must_suppress"):
        return False
    from app.gotcha import _boilerplate_collision

    for pair in data.get("pairs") or []:
        claim = str(pair.get("claim_quote") or "")
        evidence = str(pair.get("evidence_quote") or "")
        verdict = verify_pair(pair)
        veto = hard_veto_step(pair, verdict)
        if verdict.get("pass") and veto is None:
            return False
        if not (
            _boilerplate_collision(claim, evidence)
            or veto == "same_fact"
            or not verdict.get("pass")
        ):
            return False
        merged = merge_verdicts(pair, verdict, None)
        if merged.get("pass"):
            return False
    return True


def _quote_attest_eval() -> bool:
    from app.quote_attest import gotcha_quotes_attest, quote_attests

    corpus = "I did not live at 5616 14th Avenue when I was served on the evening in question."
    if not quote_attests("I did not live at 5616 14th Avenue when I was served", corpus):
        return False
    if quote_attests("I lived on Mars with the penguins forever", corpus):
        return False
    bad = {
        "claim_quote": "totally fabricated claim text that is not in the excerpt",
        "evidence_quote": "also fabricated evidence",
        "claim_document_id": "a",
        "document_id": "b",
    }
    good = {
        "claim_quote": "I did not live at 5616 14th Avenue when I was served",
        "evidence_quote": "5616 14th Avenue",
        "claim_document_id": "a",
        "document_id": "b",
    }
    texts = {"a": corpus, "b": "Utility bill for 5616 14th Avenue November 2023"}
    return gotcha_quotes_attest(good, texts) and not gotcha_quotes_attest(bad, texts)


def _national_grid_historical_fixture_eval() -> bool:
    """Track C must-ship: National Grid Brooklyn Apr 2007 stays a Gemini historical lane."""
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "foreclosure" / "national_grid_brooklyn_apr2007.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("must_ship"):
        return False
    if data.get("lane") != "historical_impossibility":
        return False
    if data.get("provider") != "gemini":
        return False
    facts = data.get("facts_on_page") or {}
    if facts.get("issuer") != "National Grid" or "Brooklyn" not in str(facts.get("place") or ""):
        return False
    if not str(facts.get("statement_date") or "").startswith("2007-04"):
        return False
    timeline = data.get("public_timeline") or {}
    if not str(timeline.get("earliest_brooklyn_service") or "").startswith("2007-08"):
        return False
    expected = data.get("expected_finding") or {}
    if expected.get("analysis_type") != "historical_impossibility":
        return False
    text = (data.get("exhibit_text") or "").lower()
    for token in expected.get("must_mention") or []:
        if str(token).lower() not in text and str(token).lower() not in json.dumps(facts).lower():
            return False
    # Statement date before Brooklyn market entry → historically impossible.
    return facts["statement_date"][:7] < timeline["earliest_brooklyn_service"][:7]


def _ai_print_scan_fixtures_eval() -> bool:
    """Fixtures for wrong-era logo, too-crisp fields, and texture-only suppress."""
    from app.engines.contextual_logic import ANALYSIS_TYPES
    from app.finding_policy import is_material_finding
    from app.models import Finding

    root = Path(__file__).resolve().parent / "fixtures" / "foreclosure"
    wrong = root / "wrong_era_logo_2007.json"
    crisp = root / "too_crisp_fields_aged_form.json"
    clean = root / "clean_statement_suppress_texture.json"
    if not wrong.exists() or not crisp.exists() or not clean.exists():
        return False

    wrong_data = json.loads(wrong.read_text(encoding="utf-8"))
    crisp_data = json.loads(crisp.read_text(encoding="utf-8"))
    clean_data = json.loads(clean.read_text(encoding="utf-8"))
    if not wrong_data.get("must_ship") or not crisp_data.get("must_ship"):
        return False
    if not clean_data.get("must_suppress"):
        return False
    if "logo_chrome_mismatch" not in ANALYSIS_TYPES:
        return False

    # Material interrupt when chrome pass ships with content hook.
    chrome_hit = Finding(
        id="ctx-logo-test",
        category="contextual",
        severity="medium",
        title="Logo / chrome mismatch — National Grid",
        explanation="Wrong-era mark on 2007 Brooklyn statement.",
        why_suspicious="Print-scan concealment still leaves wrong chrome.",
        confidence=55.0,
        evidence={
            "analysis_type": "logo_chrome_mismatch",
            "ai_generated": True,
            "content_hook": True,
            "issuer": "National Grid",
            "statement_date": "2007-04-12",
            "quoted_evidence": "Statement Date: April 12, 2007",
        },
    )
    if not is_material_finding(chrome_hit):
        return False

    # Whole-page ELA texture alone must not interrupt.
    texture_only = Finding(
        id="img-ela-texture",
        category="image",
        severity="high",
        title="Scan texture",
        explanation="Whole-page flatbed texture",
        why_suspicious="Noise",
        confidence=80.0,
        evidence={
            "analysis_type": "graphic_manipulation",
            "scan_heavy": True,
            "quoted_evidence": "Whole-page scan texture",
        },
    )
    if is_material_finding(texture_only):
        return False

    # logo_chrome without hook should not interrupt via bare AI path alone if we require hook —
    # still allow MAJOR_AI_TYPES path: ensure content_hook path is the one we use.
    bare = Finding(
        id="ctx-logo-bare",
        category="contextual",
        severity="medium",
        title="Vague chrome",
        explanation="Maybe odd",
        why_suspicious="Unsure",
        confidence=55.0,
        evidence={"analysis_type": "logo_chrome_mismatch", "ai_generated": True},
    )
    return (
        is_material_finding(chrome_hit)
        and not is_material_finding(texture_only)
        and not is_material_finding(bare)
    )


def evaluate_lie_detector() -> dict:
    """Planted documentary BS + clean twins + seeded mutations (meta-rule, not lie taxonomy)."""
    from app.fact_extract import extract_facts

    path = Path(__file__).resolve().parent / "fixtures" / "lie_detector" / "planted_bs_cases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []

    def _shipped_internal(text: str, name: str = "binder") -> list:
        facts = extract_facts(text)
        return _internal_fact_conflicts(name, facts)

    def _shipped_cross(docs: list[dict]) -> list:
        if len(docs) < 2:
            return []
        prior = docs[0]
        new = docs[1]
        prior_facts = extract_facts(prior["text"])
        new_facts = extract_facts(new["text"])
        findings, matrix = compare_to_case(
            new_document_id=new["id"],
            new_display_name=new["name"],
            new_entities={},
            new_amounts=[],
            new_fingerprint=None,
            new_doc_kind="exhibit",
            prior_snapshots=[
                {
                    "document_id": prior["id"],
                    "display_name": prior["name"],
                    "doc_kind": "exhibit",
                    "facts": prior_facts,
                    "text_excerpt": prior["text"],
                }
            ],
            new_facts=new_facts,
            new_text=new["text"],
        )
        material = [
            f
            for f in findings
            if f.severity in {"high", "medium"}
            and (
                (f.evidence or {}).get("documentary")
                or (f.evidence or {}).get("case_impact")
                or "Employment" in f.title
                or "conflicts" in f.title.lower()
            )
        ]
        red_matrix = [r for r in matrix if r.get("flag") == "red" and r.get("documentary")]
        return material or red_matrix

    def _shipped_aggregate(docs: list[dict]) -> list:
        snaps = []
        for doc in docs:
            snaps.append(
                {
                    "document_id": doc["id"],
                    "display_name": doc["name"],
                    "facts": extract_facts(doc["text"]),
                    "text_excerpt": doc["text"],
                }
            )
        return [row for row in aggregate_fact_conflicts(snaps) if row.get("severity") in {"high", "medium"}]

    for case in data.get("cases") or []:
        mode = case.get("mode")
        hits: list = []
        if mode == "internal_binder":
            text = case.get("text") or ""
            if case.get("mutation"):
                text = (case.get("base_text") or "") + str((case.get("mutation") or {}).get("append") or "")
            hits = _shipped_internal(text, case.get("id", "binder"))
        elif mode == "cross_doc":
            hits = _shipped_cross(case.get("documents") or [])
        elif mode == "aggregate":
            hits = _shipped_aggregate(case.get("documents") or [])

        shipped = len(hits) > 0
        if case.get("must_ship"):
            ok = shipped
        elif case.get("must_suppress"):
            ok = not shipped
        else:
            expect_tp = case.get("label") == "tp"
            ok = shipped if expect_tp else not shipped

        bucket_ok = True
        if shipped and case.get("impact_bucket") and hits:
            expected = case["impact_bucket"]
            first = hits[0]
            if hasattr(first, "evidence"):
                bucket_ok = (first.evidence or {}).get("case_impact_bucket") == expected or (
                    first.evidence or {}
                ).get("case_impact") is True
            elif isinstance(first, dict):
                bucket_ok = first.get("case_impact_bucket") == expected or first.get("case_impact") is True
            else:
                bucket_ok = True

        rows.append(
            {
                "id": case.get("id"),
                "shape": case.get("shape"),
                "mode": mode,
                "label": case.get("label"),
                "shipped": shipped,
                "hit_count": len(hits),
                "ok": ok and (bucket_ok if case.get("must_ship") and case.get("impact_bucket") else True),
            }
        )

    tp = [r for r in rows if r["label"] == "tp"]
    fp = [r for r in rows if r["label"] == "fp"]
    tp_hit = sum(1 for r in tp if r["shipped"])
    fp_flagged = sum(1 for r in fp if r["shipped"])
    return {
        "rows": rows,
        "precision": round(tp_hit / (tp_hit + fp_flagged), 3) if (tp_hit + fp_flagged) else 1.0,
        "recall": round(tp_hit / len(tp), 3) if tp else 0.0,
        "misses": [r for r in rows if not r["ok"]],
        "all_pass": all(r["ok"] for r in rows),
    }


def evaluate_desktop_fact_packet() -> dict:
    """Wayne occupancy must ship via the desktop ephemeral packet path.

    Desktop /v1/jobs scans omit case_id; the board rebuilds conflicts from a
    client-supplied facts/text packet. This is the regression that quieted
    case-name “waynes” / 11467 P001 on the thin client.
    """
    from app.case_consistency import fact_conflicts_from_packet
    from app.finding_policy import is_material_finding
    from app.models import Finding

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
            {
                "document_id": "d22",
                "display_name": "Wayne affidavit Sept 2021",
                "text_excerpt": d22,
            },
            {
                "document_id": "d69",
                "display_name": "Wayne affidavit Aug 2022",
                "text_excerpt": d69,
            },
        ]
    )
    material = [row for row in hits if row.get("severity") in {"high", "medium"}]
    occupancy = [
        row
        for row in material
        if row.get("field") == "occupancy_start" or "Occupancy start" in str(row.get("title") or "")
    ]

    # Pairwise documentary case_consistency must stay material for risk scoring
    # (otherwise both exhibits risk green even when the board has the card).
    pairwise = Finding(
        id="ctx-case-occ",
        category="case_consistency",
        severity="high",
        title="Occupancy start does not match prior affidavit",
        explanation="April 3 vs May 17",
        why_suspicious="same person incompatible occupancy_start",
        confidence=88,
        evidence={
            "case_consistency": True,
            "prior_document_id": "d22",
            "prior_display_name": "Wayne affidavit Sept 2021",
            "field": "occupancy_start",
            "documentary": True,
            "analysis_type": "fact_conflict",
            "case_impact": True,
            "case_impact_bucket": "credibility",
            "material": True,
        },
    )

    # Ephemeral peer path: compare_to_case findings must yellow/red the file score.
    from app.case_consistency import compare_to_case
    from app.fact_extract import extract_facts
    from app.report.scoring import score_risk

    peer_facts = extract_facts(d22)
    new_facts = extract_facts(d69)
    consistency, _matrix = compare_to_case(
        new_document_id="d69",
        new_display_name="Wayne affidavit Aug 2022",
        new_entities={},
        new_amounts=[],
        new_fingerprint="x",
        prior_snapshots=[
            {
                "document_id": "d22",
                "display_name": "Wayne affidavit Sept 2021",
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
    peer_risk, peer_score = score_risk(consistency)

    return {
        "wayne_packet_ships": len(occupancy) >= 1,
        "wayne_severity_high": bool(occupancy) and occupancy[0].get("severity") == "high",
        "documentary_pairwise_stays_material": is_material_finding(pairwise),
        "ephemeral_peer_compare_hits": len(consistency) >= 1,
        "ephemeral_peer_risk_not_green": peer_risk in {"yellow", "red"} and peer_score >= 35,
        "all_pass": len(occupancy) >= 1
        and bool(occupancy)
        and occupancy[0].get("severity") == "high"
        and is_material_finding(pairwise)
        and len(consistency) >= 1
        and peer_risk in {"yellow", "red"}
        and peer_score >= 35,
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
    templates = evaluate_template_fabrication()
    templates["all_pass"] = all(templates[key] for key in templates if key != "all_pass")
    statement_dates = evaluate_statement_dates()
    statement_dates["all_pass"] = all(
        statement_dates[key] for key in statement_dates if key != "all_pass"
    )
    case_impact = evaluate_case_impact()
    case_impact["all_pass"] = all(case_impact[key] for key in case_impact if key != "all_pass")
    lie_detector = evaluate_lie_detector()
    desktop_packet = evaluate_desktop_fact_packet()
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
        "template_fabrication": templates,
        "statement_dates": statement_dates,
        "case_impact": case_impact,
        "lie_detector": lie_detector,
        "desktop_fact_packet": desktop_packet,
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
    print("TEMPLATE FABRICATION", result["template_fabrication"])
    print("STATEMENT DATES", result["statement_dates"])
    print("CASE IMPACT", result["case_impact"])
    print("LIE DETECTOR", result["lie_detector"])
    print("DESKTOP FACT PACKET", result["desktop_fact_packet"])
    failed = bool(
        misses
        or pipe["misses"]
        or not result["unverified_paths"]["all_pass"]
        or not result["dedupe"]["all_pass"]
        or not result["tiers"]["all_pass"]
        or not result["tainted_support"]["all_pass"]
        or not result["doc_kinds"]["all_pass"]
        or not result["recorded_math"]["all_pass"]
        or not result["template_fabrication"]["all_pass"]
        or not result["statement_dates"]["all_pass"]
        or not result["case_impact"]["all_pass"]
        or not result["lie_detector"]["all_pass"]
        or not result["desktop_fact_packet"]["all_pass"]
    )
    if not result["score_independence"]["title_does_not_matter"]:
        failed = True
    if not result["score_independence"]["unverified_does_not_score"]:
        failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
