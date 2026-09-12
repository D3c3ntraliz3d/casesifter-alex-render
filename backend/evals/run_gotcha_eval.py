"""Cross-practice gotcha verification eval. Run from backend/: python -m evals.run_gotcha_eval"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from app.gotcha import verify_pair
from app.models import Finding
from app.report.scoring import score_risk


def load_cases() -> dict:
    path = Path(__file__).with_name("gotcha_regression.json")
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate() -> dict:
    data = load_cases()
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


def score_independence() -> dict:
    """Same document type, different verified findings → different scores."""
    empty = score_risk([])
    gotcha = Finding(
        id="t-gotcha",
        category="gotcha",
        severity="high",
        title="GOTCHA",
        explanation="x",
        why_suspicious="y",
        confidence=88,
        evidence={
            "gotcha": True,
            "verified": True,
            "contradiction_type": "procedural_foundational",
            "material": True,
        },
    )
    hot = score_risk([gotcha])
    return {
        "empty_affidavit": empty,
        "affidavit_with_gotcha": hot,
        "differs": empty != hot,
    }


def main() -> None:
    result = evaluate()
    print("PER AREA")
    for area, stats in result["summary"].items():
        print(f"  {area}: precision={stats['precision']} recall={stats['recall']} {stats}")
    misses = [row for row in result["rows"] if not row["ok"]]
    print("MISSES", len(misses))
    for row in misses:
        print(" ", row)
    print("SCORE INDEPENDENCE", score_independence())


if __name__ == "__main__":
    main()
