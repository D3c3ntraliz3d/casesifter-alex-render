"""Image forensics dependency probe stays cheap and honest."""

from __future__ import annotations

from app.image_deps import missing_image_forensics_deps, require_image_forensics_deps
from app.scan_signals import scan_signals


def test_missing_deps_is_list():
    missing = missing_image_forensics_deps()
    assert isinstance(missing, list)


def test_require_deps_when_present():
    if missing_image_forensics_deps():
        return
    require_image_forensics_deps()


def test_scan_signals_passes_image_error():
    report = {
        "modules": [
            {
                "name": "image_forensics",
                "status": "error",
                "summary": "Module failed: No module named 'cv2'",
                "extras": {"error": "No module named 'cv2'"},
            },
            {"name": "ocr_text", "status": "ok"},
        ]
    }
    signals = scan_signals(report)
    img = next(m for m in signals["scan_modules"] if m["name"] == "image_forensics")
    assert img["status"] == "error"
    assert "cv2" in img["error"]
    ok = next(m for m in signals["scan_modules"] if m["name"] == "ocr_text")
    assert "error" not in ok
    assert "Image error" in signals["profile_tags"]


def test_scan_signals_profile_tags_from_findings():
    report = {
        "ai_review": "incomplete",
        "findings": [
            {
                "severity": "high",
                "evidence": {"analysis_type": "date_inconsistency"},
            },
            {
                "severity": "medium",
                "evidence": {"analysis_type": "historical_impossibility"},
            },
            {
                "severity": "high",
                "evidence": {"analysis_type": "math_error"},
            },
            {
                "severity": "info",
                "evidence": {"analysis_type": "font_mismatch"},
            },
        ],
        "modules": [{"name": "ocr_text", "status": "ok"}],
    }
    tags = scan_signals(report)["profile_tags"]
    assert "Statement dates" in tags
    assert "Issuer era" in tags
    assert "Statement math" in tags
    assert "AI review incomplete" in tags
    assert "Font mismatch" not in tags  # info-only suppressed when stronger tags exist


def test_scan_signals_data_profile_and_info_summary():
    report = {
        "executive_summary": (
            "“S_41.pdf” is assessed as high risk. The leading concern is: "
            "Verizon statement date conflict. Review each cited page."
        ),
        "risk_level": "red",
        "comparison": {
            "entities": {
                "organizations": ["Verizon", "National Grid", "Citibank"],
                "people": ["John Doe"],
            },
            "facts": [
                {"key": "notice_date", "kind": "date", "display": "April 12, 2007"},
                {"key": "outstanding_balance", "kind": "money", "display": "$2,075.38"},
                {"key": "residence_address", "kind": "address", "display": "100 Main St"},
            ],
            "doc_kind": "exhibit",
        },
        "findings": [
            {
                "title": "Verizon statement date conflict",
                "severity": "high",
                "evidence": {"analysis_type": "date_inconsistency"},
            },
            {
                "title": "National Grid market era",
                "severity": "high",
                "evidence": {"analysis_type": "historical_impossibility"},
            },
        ],
        "modules": [{"name": "ocr_text", "status": "ok"}],
    }
    signals = scan_signals(report, page_count=5, doc_kind="exhibit")
    profile = signals["data_profile"]
    assert profile["pages"] == 5
    assert "Verizon" in profile["organizations"]
    assert "April 12, 2007" in profile["dates"]
    assert "$2,075.38" in profile["amounts"]
    assert profile["finding_counts"].get("high") == 2
    assert signals["info_summary"]
    assert "high risk" in signals["info_summary"].lower() or "Verizon" in signals["info_summary"]
    assert "Statement dates" in signals["profile_tags"]
