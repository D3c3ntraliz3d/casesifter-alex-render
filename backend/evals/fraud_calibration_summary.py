#!/usr/bin/env python3
"""Aggregate precision / suppress rates from in-repo forgery fixture packs.

Run from backend/:

  python -m evals.fraud_calibration_summary

Or:

  pytest -q evals/test_s41_statement_signals.py evals/test_ela_l1_promote.py \\
         evals/test_forgery_risk_floor.py evals/test_logo_chrome_must_suppress.py \\
         evals/test_visual_review_fail_closed.py

This script does not call Gemini. It scores deterministic L1 packs and ELA gates.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from app.engines.statement_body import (
    find_issuer_market_anachronisms,
    find_line_self_contradictions,
    find_statement_math_errors,
)
from app.engines.statement_dates import find_statement_date_conflicts
from app.engines.template_fabrication import review_print_scan_ela
from app.finding_policy import is_material_finding
from app.models import Finding

# Reuse fixture text from the S-41 pack without importing pytest parametrize noise.
from evals import test_s41_statement_signals as s41
from evals import test_ela_l1_promote as ela


@dataclass
class Case:
    pack: str
    name: str
    expect_fire: bool
    run: Callable[[], bool]


def _any_material(findings: list[Finding]) -> bool:
    return any(is_material_finding(item) for item in findings)


def _l1_fire(text: str) -> bool:
    hits: list[Finding] = []
    hits.extend(find_statement_date_conflicts(text, scan_profile="forensic", source="cal"))
    hits.extend(find_issuer_market_anachronisms(text, scan_profile="forensic", source="cal"))
    hits.extend(find_line_self_contradictions(text, scan_profile="forensic", source="cal"))
    hits.extend(find_statement_math_errors(text, scan_profile="forensic", source="cal"))
    return _any_material(hits)


def _build_cases() -> list[Case]:
    must_fire = [
        ("grid_brooklyn", s41.NATIONAL_GRID_BROOKLYN),
        ("grid_718", s41.NATIONAL_GRID_718),
        ("grid_glued", s41.NATIONAL_GRID_GLUED_PHONE),
        ("grid_212", s41.NATIONAL_GRID_212_GLUE),
        ("citi_broken_math", s41.CITI_BROKEN_MATH),
        ("citi_glued_math", s41.CITI_GLUED_MATH),
        ("citi_line", s41.CITI_LINE),
        ("verizon_forward", s41.VERIZON_BROKEN_FORWARD),
        ("verizon_glued_forward", s41.VERIZON_GLUED_FORWARD),
        ("verizon_amount_due", s41.VERIZON_AMOUNT_DUE_IDENTITY),
        ("verizon_payment", s41.VERIZON_PAYMENT),
        ("pseg_early", s41.PSEG_LI_TOO_EARLY),
        ("pseg_glued", s41.PSEG_LI_GLUED),
    ]
    must_suppress = [
        ("grid_upstate", s41.NATIONAL_GRID_UPSTATE),
        ("grid_after_close", s41.NATIONAL_GRID_AFTER_CLOSE),
        ("citi_reconciled", s41.CITI_ONE_SPELLING),
        ("verizon_reconciled_identity", s41.VERIZON_RECONCILED_IDENTITY),
        ("verizon_prior_year_payment", s41.VERIZON_PRIOR_YEAR),
        ("pseg_after_start", s41.PSEG_LI_AFTER_START),
    ]
    cases: list[Case] = []
    for name, text in must_fire:
        cases.append(Case("must_fire", name, True, lambda t=text: _l1_fire(t)))
    for name, text in must_suppress:
        cases.append(Case("must_suppress", name, False, lambda t=text: _l1_fire(t)))

    def ela_clean_alone() -> bool:
        upgraded = review_print_scan_ela([ela._ela(page=1)], ela.CLEAN, scan_profile="forensic")
        return _any_material(upgraded)

    def ela_with_template() -> bool:
        upgraded = review_print_scan_ela([ela._ela(page=1)], ela.TEMPLATE, scan_profile="forensic")
        return _any_material(upgraded)

    def ela_with_same_page_l1() -> bool:
        upgraded = review_print_scan_ela(
            [ela._ela(page=2), ela._l1_math(page=2)], ela.CLEAN, scan_profile="forensic"
        )
        return any(
            is_material_finding(item)
            for item in upgraded
            if str(item.id).startswith("img-ela-")
        )

    def ela_other_page_l1() -> bool:
        upgraded = review_print_scan_ela(
            [ela._ela(page=1), ela._l1_math(page=2)], ela.CLEAN, scan_profile="forensic"
        )
        return any(
            is_material_finding(item)
            for item in upgraded
            if str(item.id).startswith("img-ela-")
        )

    cases.extend(
        [
            Case("ela_gate", "clean_strong_ela_alone", False, ela_clean_alone),
            Case("ela_gate", "template_plus_ela", True, ela_with_template),
            Case("ela_gate", "same_page_l1_plus_ela", True, ela_with_same_page_l1),
            Case("ela_gate", "other_page_l1_plus_ela", False, ela_other_page_l1),
        ]
    )
    return cases


def main() -> int:
    cases = _build_cases()
    by_pack: dict[str, list[tuple[Case, bool, bool]]] = {}
    for case in cases:
        fired = bool(case.run())
        ok = fired == case.expect_fire
        by_pack.setdefault(case.pack, []).append((case, fired, ok))

    print("Fraud calibration summary (deterministic packs)")
    print("=" * 56)
    total_ok = 0
    total = 0
    for pack, rows in by_pack.items():
        ok_n = sum(1 for _c, _f, ok in rows if ok)
        total_ok += ok_n
        total += len(rows)
        fire_n = sum(1 for c, fired, _ok in rows if c.expect_fire and fired)
        fire_t = sum(1 for c, _f, _ok in rows if c.expect_fire)
        suppress_n = sum(1 for c, fired, _ok in rows if not c.expect_fire and not fired)
        suppress_t = sum(1 for c, _f, _ok in rows if not c.expect_fire)
        print(f"\n[{pack}] {ok_n}/{len(rows)} cases ok")
        if fire_t:
            print(f"  must_fire precision@pack: {fire_n}/{fire_t} fired as expected")
        if suppress_t:
            print(f"  must_suppress rate:       {suppress_n}/{suppress_t} stayed quiet")
        for case, fired, ok in rows:
            mark = "OK" if ok else "FAIL"
            expect = "fire" if case.expect_fire else "suppress"
            got = "fire" if fired else "quiet"
            print(f"  {mark:4} {case.name:32} expect={expect:8} got={got}")

    print("\n" + "=" * 56)
    print(f"TOTAL {total_ok}/{total} ok")
    if total_ok != total:
        print("Calibration pack failed — see FAIL rows above.")
        return 1
    print("All packs passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
