"""Meta must not crash the row Incomplete on common e-file date junk."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, create_string_object

from app.engines.metadata import _pdf_date, _pdf_info_date, analyze_metadata
from app.scan_signals import profile_tags, scan_signals


def test_pdf_date_strips_trailing_quote():
    assert _pdf_date("D:20260211214524Z'") == "2026-02-11T21:45:24+00:00"
    assert _pdf_date("D:20251029141340Z'") == "2025-10-29T14:13:40+00:00"


def test_pdf_info_date_reads_raw_without_typed_property(tmp_path: Path):
    """Simulate Info dates that make pypdf's creation_date property raise."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    info = writer._info  # noqa: SLF001 — test fixture only
    info[NameObject("/CreationDate")] = create_string_object("D:20260211214524Z'")
    info[NameObject("/ModDate")] = create_string_object("D:20251029141340Z'")
    buf = BytesIO()
    writer.write(buf)
    path = tmp_path / "junk_dates.pdf"
    path.write_bytes(buf.getvalue())

    reader = PdfReader(str(path))
    meta_info = reader.metadata
    assert meta_info is not None
    with pytest.raises(ValueError, match="Can not convert date"):
        _ = meta_info.creation_date

    from pypdf.constants import DocumentInformationAttributes as DIA

    assert _pdf_info_date(meta_info, DIA.CREATION_DATE) == "2026-02-11T21:45:24+00:00"
    assert _pdf_info_date(meta_info, DIA.MOD_DATE) == "2025-10-29T14:13:40+00:00"

    result = analyze_metadata(path)
    assert result.status == "ok"
    assert (result.extras or {}).get("error") is None
    doc = ((result.extras or {}).get("metadata") or {}).get("document") or {}
    assert doc.get("created") == "2026-02-11T21:45:24+00:00"
    assert doc.get("modified") == "2025-10-29T14:13:40+00:00"


def test_meta_module_error_is_soft_fail_warning_tag():
    """Meta status=error must surface as Meta warning, not block like Image error."""
    report = {
        "risk_level": "green",
        "modules": [
            {
                "name": "metadata",
                "status": "error",
                "summary": "Module failed: Can not convert date: D:20260211214524Z'",
                "extras": {"error": "Can not convert date: D:20260211214524Z'"},
            },
            {"name": "ocr_text", "status": "ok"},
            {"name": "contextual_logic", "status": "ok"},
        ],
    }
    signals = scan_signals(report)
    meta = next(m for m in signals["scan_modules"] if m["name"] == "metadata")
    assert meta["status"] == "error"
    assert "Can not convert date" in meta["error"]
    tags = signals["profile_tags"]
    assert "Meta warning" in tags
    assert "Meta error" not in tags
    # Quiet warning chip only — no Incomplete / Review incomplete from Meta alone.
    assert "Review incomplete" not in tags
    assert "AI review incomplete" not in tags
    assert profile_tags(report) == tags


def test_image_error_still_hard_fail_tag():
    report = {
        "modules": [
            {"name": "metadata", "status": "ok"},
            {
                "name": "image_forensics",
                "status": "error",
                "extras": {"error": "No module named 'cv2'"},
            },
        ],
    }
    tags = scan_signals(report)["profile_tags"]
    assert "Image error" in tags
    assert "Meta warning" not in tags


def test_analyze_metadata_retries_once_on_error(tmp_path: Path):
    """First Meta crash is retried once; second success returns ok."""
    path = tmp_path / "retry.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())

    calls = {"n": 0}

    def _boom_then_ok(_path: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("Can not convert date: D:20260211214524Z'")
        return [], {"metadata": {"document": {}}, "timeline": []}

    with patch("app.engines.metadata._run", side_effect=_boom_then_ok):
        result = analyze_metadata(path)
    assert calls["n"] == 2
    assert result.status == "ok"


def test_analyze_metadata_soft_fails_after_retry(tmp_path: Path):
    """Persistent Meta crash stays status=error (clearance soft-fails in UI)."""
    path = tmp_path / "fail.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())

    calls = {"n": 0}

    def _always_boom(_path: Path):
        calls["n"] += 1
        raise ValueError("Can not convert date: D:20260211214524Z'")

    with patch("app.engines.metadata._run", side_effect=_always_boom):
        result = analyze_metadata(path)
    assert calls["n"] == 2
    assert result.status == "error"
    assert "Can not convert date" in (result.extras or {}).get("error", "")
