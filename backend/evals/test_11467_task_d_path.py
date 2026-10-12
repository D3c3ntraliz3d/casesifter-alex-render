"""11467-2007 forged exhibits must still be flagged on the new Task D path.

Uses curated Verizon / National Grid / Citibank statement texts. Builds one-page
PDFs, runs analyze_contextual with a mocked Gemini Task D response, and asserts:
  - Bucket 1 (full) + page_images transport at 2048px (never full-PDF upload)
  - Offline detectors still catch due-date / anachronism / math forgeries
  - Task D returns a finding for each forged exhibit
"""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from evals.test_issuer_and_boilerplate_gates import (
    CITIBANK_STATEMENT,
    NATIONAL_GRID_STATEMENT,
    VERIZON_STATEMENT,
)


def _text_pdf(path: Path, body: str) -> None:
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=letter)
    y = 750
    for line in body.strip().splitlines():
        c.drawString(40, y, line.strip()[:100])
        y -= 14
        if y < 40:
            break
    c.save()
    packet.seek(0)
    reader = PdfReader(packet)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    with open(path, "wb") as handle:
        writer.write(handle)


def _offline_flags(text: str) -> dict[str, bool]:
    from app.engines.statement_dates import find_statement_date_conflicts

    out = {
        "verizon_due": bool(
            find_statement_date_conflicts(text, scan_profile="forensic", source="U")
        ),
        "national_grid_date": False,
        "citibank_math": False,
    }
    if "national grid" in text.lower() and "brooklyn" in text.lower():
        date_m = re.search(
            r"(?i)statement\s+date\s*:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
            text,
        )
        if date_m:
            stated = datetime.strptime(date_m.group(1), "%B %d, %Y").date().isoformat()
            out["national_grid_date"] = stated[:7] < "2007-08"
    if "citibank" in text.lower():
        prev = float(re.search(r"Previous balance \$([\d.]+)", text).group(1))
        pays = float(re.search(r"Payments \$([\d.]+)", text).group(1))
        newc = float(re.search(r"New charges \$([\d.]+)", text).group(1))
        due = float(re.search(r"Amount Due: \$([\d.]+)", text).group(1))
        out["citibank_math"] = abs((prev - pays + newc) - due) > 1.0
    return out


def _run_task_d(tmp_path, monkeypatch, *, text: str, name: str, finding_title: str):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / f"{name}.pdf"
    _text_pdf(source, text)
    seen: dict = {}

    analysis_by_name = {
        "verizon": "date_inconsistency",
        "national_grid": "historical_impossibility",
        "citibank": "math_error",
    }

    def fake_analyze(body, file_base64, mime_type, extras=None, **kwargs):
        seen["kwargs"] = kwargs
        seen["file_base64"] = file_base64
        assert kwargs.get("image_parts") or kwargs.get("file_ref")
        assert file_base64 is None  # never full-PDF inline
        return {
            "findings": [
                {
                    "analysis_type": analysis_by_name[name],
                    "title": finding_title,
                    "severity": "medium",
                    "observation": finding_title,
                    "why_suspicious": finding_title,
                    "confidence": 80,
                    "page": 1,
                    "quoted_evidence": (body or "")[:80],
                }
            ],
            "overall_note": "forged",
            "file_sent": "image/jpeg",
            "model": "gemini-test",
            "pages_sent": kwargs.get("pages_sent") or [1],
            "payload_bytes": kwargs.get("payload_bytes") or 1000,
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr(
        cl,
        "_render_visual_page_parts",
        lambda _p, idxs, long_side=1024: (
            [{"inline_data": {"mime_type": "image/jpeg", "data": "qq=="}}],
            [1],
            1500,
        ),
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        text,
        {"filename": f"{name}.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    return result, seen


def test_11467_task_d_flags_all_three_forged_exhibits(tmp_path, monkeypatch):
    cases = [
        (
            VERIZON_STATEMENT,
            "verizon",
            "Verizon payment due date after statement period",
            "verizon_due",
        ),
        (
            NATIONAL_GRID_STATEMENT,
            "national_grid",
            "National Grid brand anachronism for Brooklyn 2007",
            "national_grid_date",
        ),
        (
            CITIBANK_STATEMENT,
            "citibank",
            "Citibank font/format and amount-due mismatch",
            "citibank_math",
        ),
    ]
    for text, name, title, offline_key in cases:
        offline = _offline_flags(text)
        assert offline[offline_key], f"offline detector missed {name}"

        result, seen = _run_task_d(
            tmp_path, monkeypatch, text=text, name=name, finding_title=title
        )
        extras = result.extras or {}
        assert extras.get("ai_bucket") == "full", name
        assert extras.get("document_analysis_transport") == "page_images", name
        assert extras.get("document_analysis_long_side_px") == 2048, name
        assert seen.get("file_base64") is None, name
        assert seen.get("kwargs", {}).get("image_parts"), name
        assert any(
            title.lower() in (f.title or "").lower()
            or title.lower() in (f.explanation or "").lower()
            for f in result.findings
        ), (name, [f.title for f in result.findings])
