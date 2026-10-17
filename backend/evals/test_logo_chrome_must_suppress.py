"""Golden must_suppress fixtures for logo/chrome when Gemini is enabled.

Skips cleanly without GEMINI_API_KEY or when multimodal/logo chrome is off.
Also locks the quote gate so hallucinated mismatches without evidence stay quiet.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.finding_policy import is_material_finding
from app.models import Finding

pytestmark = pytest.mark.integration


def _gemini_ready() -> bool:
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        return False
    try:
        from app.config import settings

        return bool(settings.gemini_api_key and settings.contextual_multimodal and settings.logo_chrome_pass)
    except Exception:
        return False


def test_logo_mismatch_without_quote_is_not_material():
    weak = Finding(
        id="ctx-logo-weak",
        category="contextual",
        severity="medium",
        title="Logo / chrome mismatch — Chase",
        explanation="Issuer looks wrong for the era.",
        why_suspicious="Model thinks the mark is modern.",
        confidence=55,
        evidence={
            "analysis_type": "logo_chrome_mismatch",
            "ai_generated": True,
            "issuer": "Chase",
            "quoted_evidence": "",
            "logo_note": "short",
            "content_hook": False,
        },
    )
    assert is_material_finding(weak) is False


def test_open_ended_ai_historical_without_curated_issuer_is_not_material():
    loose = Finding(
        id="ctx-hist-loose",
        category="contextual",
        severity="medium",
        title="Historical / market timeline conflict",
        explanation="This fictional bank did not exist in Atlantis in 1800.",
        why_suspicious="Model invents an era conflict.",
        confidence=70,
        evidence={
            "analysis_type": "historical_impossibility",
            "ai_generated": True,
            "quoted_evidence": "Atlantis Bank Statement Date: April 1, 1800",
        },
    )
    assert is_material_finding(loose) is False

    grounded = Finding(
        id="ctx-hist-grid",
        category="contextual",
        severity="medium",
        title="National Grid did not serve Brooklyn in April 2007",
        explanation="National Grid began serving Brooklyn after KeySpan closed.",
        why_suspicious="Curated issuer era conflict.",
        confidence=72,
        evidence={
            "analysis_type": "historical_impossibility",
            "ai_generated": True,
            "quoted_evidence": "Statement Date: April 12, 2007. Service Address: Brooklyn, NY. National Grid",
        },
    )
    assert is_material_finding(grounded)


@pytest.mark.skipif(not _gemini_ready(), reason="GEMINI_API_KEY / multimodal logo chrome not configured")
def test_clean_consumer_statement_logo_chrome_must_suppress(tmp_path: Path, monkeypatch):
    """Live Gemini pass on a clean decoy raster must not invent a material mismatch."""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont

    from app.config import settings
    from app.engines.logo_chrome import run_logo_chrome_check

    monkeypatch.setattr(settings, "logo_chrome_pass", True, raising=False)
    monkeypatch.setattr(settings, "contextual_multimodal", True, raising=False)

    img = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    lines = [
        "Con Edison",
        "Account Statement",
        "Account number 44122918",
        "Statement date April 16, 2007",
        "Service period April 1, 2007 - April 30, 2007",
        "Amount due $92.40",
        "Due date May 16, 2007",
    ]
    y = 40
    for line in lines:
        draw.text((40, y), line, fill="black", font=font)
        y += 36
    path = tmp_path / "clean_coned_statement.png"
    img.save(path)

    text = "\n".join(lines)
    hits = run_logo_chrome_check(
        text,
        path,
        scan_profile="forensic",
        source="Exhibit clean-decoy",
        doc_kind="statement",
    )
    material = [item for item in hits if is_material_finding(item)]
    assert material == [], [item.title for item in material]
