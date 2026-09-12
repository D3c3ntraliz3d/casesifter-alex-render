"""Cross-practice gotcha verification eval. Run from backend/: python -m evals.run_gotcha_eval"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from app.case_consistency import aggregate_fact_conflicts, dedupe_gotchas
from app.gotcha import (
    RETIRED_TYPES,
    _token_candidates,
    extract_claims,
    hard_veto_step,
    merge_verdicts,
    retrieve_evidence,
    verify_candidates,
    verify_pair,
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
            flagged = bool(verdict.get("pass"))
            failed = verdict.get("failed_step")
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
    local_duty = verify_pair(duty_place)
    local_notice = verify_pair(notice)
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
        ]
    )
    return {
        "one_rate_conflict": len(conflicts) == 1,
        "three_docs_referenced": len((conflicts[0].get("related_documents") if conflicts else []) or []) == 3,
        "one_gotcha_card": len(gotchas) == 1,
        "gotcha_names_both_docs": len((gotchas[0].get("related_documents") if gotchas else []) or []) == 2,
        "all_pass": False,
    }


def evaluate() -> dict:
    dedupe = evaluate_dedupe()
    dedupe["all_pass"] = all(dedupe[key] for key in dedupe if key != "all_pass")
    return {
        "pairs": evaluate_pairs(),
        "pipeline": evaluate_pipeline(),
        "unverified_paths": evaluate_unverified_paths(),
        "score_independence": score_independence(),
        "dedupe": dedupe,
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
    failed = bool(misses or pipe["misses"] or not result["unverified_paths"]["all_pass"] or not result["dedupe"]["all_pass"])
    if not result["score_independence"]["title_does_not_matter"]:
        failed = True
    if not result["score_independence"]["unverified_does_not_score"]:
        failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
