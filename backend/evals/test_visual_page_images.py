"""Task D visual payload: page images, selection, Files API oversize."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


def _pdf_with_page_texts(path: Path, texts: list[str]) -> None:
    writer = PdfWriter()
    for text in texts:
        writer.add_blank_page(width=612, height=792)
        # Text extraction from blank pages is empty — selection tests use monkeypatch.
    with open(path, "wb") as handle:
        writer.write(handle)


def _fat_pdf(path: Path, target_mb: float, *, pages: int = 4) -> None:
    writer = PdfWriter()
    per = max(64_000, int((target_mb * 1024 * 1024) / max(1, pages)))
    blob = (b"SCANNED-PAGE-BYTES-" * (per // 18 + 1))[:per]
    for i in range(pages):
        writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(blob)
        stream_ref = writer._add_object(stream)
        writer.pages[i][NameObject("/CSPrivate")] = stream_ref
    with open(path, "wb") as handle:
        writer.write(handle)


def test_select_visual_pages_all_when_short():
    from app.engines.contextual_logic import _select_visual_page_indices

    texts = [f"page {i}" for i in range(8)]
    assert _select_visual_page_indices(texts) == list(range(8))


def test_select_visual_pages_cues_on_long_doc():
    from app.engines.contextual_logic import _select_visual_page_indices

    texts = ["cover"] + ["noise"] * 20
    texts[5] = "Subscribed and sworn before me Notary Public"
    texts[12] = "Account statement letterhead www.bank.com"
    assert _select_visual_page_indices(texts) == [0, 5, 12]


def test_small_pdf_uses_page_images_transport(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "short.pdf"
    _pdf_with_page_texts(source, ["a", "b", "c"])

    seen: dict = {}

    def fake_analyze(text, file_base64, mime_type, extras=None, **kwargs):
        seen["kwargs"] = kwargs
        seen["file_base64"] = file_base64
        return {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "image/jpeg",
            "model": "gemini-test",
            "pages_sent": kwargs.get("pages_sent") or [],
            "payload_bytes": kwargs.get("payload_bytes") or 0,
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr(cl, "_pdf_page_texts", lambda _p: ["p1", "p2", "p3"])
    monkeypatch.setattr(
        cl,
        "_render_visual_page_parts",
        lambda _p, idxs: (
            [{"inline_data": {"mime_type": "image/jpeg", "data": "qq=="}} for _ in idxs],
            [i + 1 for i in idxs],
            1200,
        ),
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "Account number 99 statement date January 1 2020 principal balance",
        {"filename": "stmt.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    assert (result.extras or {}).get("document_analysis_transport") == "page_images"
    assert seen["file_base64"] is None
    assert seen["kwargs"].get("image_parts")
    assert seen["kwargs"].get("pages_sent") == [1, 2, 3]
    assert (result.extras or {}).get("ai_bucket") == "full"


def test_procedural_bucket_skips_visual(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl

    source = tmp_path / "calendar.pdf"
    _pdf_with_page_texts(source, ["x"])
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("visual should not run")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl, "_run_document_analysis", boom)

    # Bucket 3 (none) body that is not also caught by boilerplate_skip_reason.
    result = cl.analyze_contextual(
        "Calendar call — this matter is adjourned to the next conference date. "
        "No appearances required. Index No. 123/2024.",
        {"filename": "calendar.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    assert called["n"] == 0
    da = str((result.extras or {}).get("document_analysis") or "")
    assert "skipped" in da


def test_over_15mb_still_files_api(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "fat.pdf"
    _fat_pdf(source, 16.0, pages=4)
    assert source.stat().st_size > 15 * 1024 * 1024

    uploaded: list[Path] = []

    def fake_upload(path, *, mime_type="application/pdf"):
        uploaded.append(Path(path))
        return {"name": "files/mock", "uri": "https://example.test/files/mock", "state": "ACTIVE"}

    def fake_analyze(text, file_base64, mime_type, extras=None, **kwargs):
        assert kwargs.get("file_ref") is not None
        return {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "application/pdf",
            "model": "gemini-test",
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "True and accurate copy annexed as Exhibit A principal balance",
        {"filename": "fat.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    assert (result.extras or {}).get("document_analysis_transport") == "files_api"
    assert uploaded and uploaded[0] == source
