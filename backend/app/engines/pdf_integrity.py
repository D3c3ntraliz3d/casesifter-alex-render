from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from pypdf.generic import DictionaryObject, IndirectObject

from app.engines import finding, timed_module
from app.ingestion import open_pdf_reader
from app.models import Finding, ModuleResult


def analyze_pdf_integrity(path: Path) -> ModuleResult:
    if path.suffix.lower() != ".pdf":
        return ModuleResult(
            name="pdf_integrity",
            status="skipped",
            summary="Not a PDF",
            extras={"skipped": True},
        )
    return timed_module("pdf_integrity", lambda: _run(path))


def _run(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    reader = open_pdf_reader(path)
    raw = path.read_bytes()
    catalog = reader.trailer.get("/Root") if reader.trailer else None
    if isinstance(catalog, IndirectObject):
        catalog = catalog.get_object()

    eof_count = raw.count(b"%%EOF")
    has_prev = bool(reader.trailer and reader.trailer.get("/Prev"))
    javascript = _has_javascript(catalog, raw)
    embedded = _list_embedded(reader)
    layers = _list_layers(catalog)
    annot_summary = _annotations(reader)
    has_form = bool(catalog and catalog.get("/AcroForm")) if isinstance(catalog, DictionaryObject) else False
    producer = ""
    if reader.metadata and reader.metadata.producer:
        producer = str(reader.metadata.producer)

    native_vs_scan = _native_or_image_pdf(reader)

    extras: dict[str, Any] = {
        "page_count": len(reader.pages),
        "pdf_header": getattr(reader, "pdf_header", None),
        "incremental_updates": max(0, eof_count - 1),
        "has_prev_xref": has_prev,
        "javascript": javascript,
        "embedded_files": embedded,
        "layers": layers,
        "annotations": annot_summary,
        "has_acroform": has_form,
        "producer": producer,
        "origin_guess": native_vs_scan,
        "encrypted": bool(reader.is_encrypted),
    }

    if eof_count >= 3 or (has_prev and eof_count >= 2):
        findings.append(
            finding(
                fid="pdf-incremental",
                category="pdf_structure",
                severity="medium",
                title="PDF was saved more than once (incremental updates)",
                explanation=(
                    f"The file contains {eof_count} %%EOF marker(s)"
                    f"{' and a /Prev cross-reference' if has_prev else ''}. "
                    "Incremental updates append changes instead of writing a brand-new file. "
                    "That is normal in some editors and also how later alterations are stored."
                ),
                why_suspicious="A ‘final’ exhibit that was incrementally updated may differ from an earlier served copy.",
                confidence=62,
                evidence={"eof_markers": eof_count, "has_prev": has_prev},
                recommended_action="Compare this bytes-on-disk file with any earlier production copy.",
            )
        )

    if javascript:
        findings.append(_javascript_finding(javascript, native_vs_scan))

    if embedded:
        findings.append(
            finding(
                fid="pdf-embedded",
                category="pdf_structure",
                severity="medium",
                title="PDF contains embedded files",
                explanation=f"Embedded attachments: {', '.join(embedded[:12])}.",
                why_suspicious="Attachments can carry a different version of the exhibit than the visible pages.",
                confidence=70,
                evidence={"files": embedded},
                recommended_action="Extract and review each attachment separately.",
            )
        )

    if layers:
        findings.append(
            finding(
                fid="pdf-layers",
                category="pdf_structure",
                severity="high",
                title="Optional content layers (OCG) are present",
                explanation=(
                    f"Layer names: {', '.join(layers[:15])}. Optional content groups can hide graphics or text "
                    "depending on the viewer’s layer panel."
                ),
                why_suspicious="Hidden layers are a well-known way to conceal content in a ‘flat’ looking PDF.",
                confidence=80,
                evidence={"layers": layers},
                recommended_action="In Adobe Acrobat, open the Layers pane and toggle every group.",
            )
        )

    if annot_summary.get("redact_unapplied"):
        findings.append(
            finding(
                fid="pdf-redact-annot",
                category="pdf_structure",
                severity="critical",
                title="Unapplied redaction annotations",
                explanation=(
                    f"{annot_summary['redact_unapplied']} /Redact annotation(s) are still present. "
                    "Until redactions are applied and flattened, the underlying text usually remains extractable."
                ),
                why_suspicious="Unapplied redactions look blacked-out but often still contain the original words.",
                confidence=93,
                evidence=annot_summary,
                recommended_action="Do not treat this file as redacted. Request a flattened, applied-redaction copy.",
            )
        )

    if annot_summary.get("possible_incomplete_redaction"):
        findings.append(
            finding(
                fid="pdf-blackbox-text",
                category="pdf_structure",
                severity="high",
                title="Black-box markup on a page that still has extractable text",
                explanation=(
                    "One or more pages combine large black rectangle annotations with copyable text. "
                    "That combination is the usual failure mode of ‘drawn’ redactions."
                ),
                why_suspicious="Visual black-out without removing text operators is not a real redaction.",
                confidence=82,
                recommended_action="Select-all / copy the page. If hidden words appear, the redaction is incomplete.",
            )
        )

    if has_form:
        findings.append(
            finding(
                fid="pdf-form",
                category="pdf_structure",
                severity="info",
                title="Interactive form fields are present",
                explanation="AcroForm fields can be edited after the exhibit was ‘finalized’ unless flattened.",
                why_suspicious="Editable fields mean values may have changed after an earlier printout or signature.",
                confidence=50,
            )
        )

    if native_vs_scan["guess"] == "image-only":
        findings.append(
            finding(
                fid="pdf-image-only",
                category="pdf_structure",
                severity="info",
                title="PDF appears to be a scanned (image-only) document",
                explanation=(
                    f"{native_vs_scan['image_pages']} of {native_vs_scan['pages']} page(s) are image-heavy "
                    "with little or no extractable text. That is normal for paper scans and also how edited "
                    "photographs are wrapped as PDFs."
                ),
                why_suspicious="Image-only PDFs hide native edit history; image-forensics results matter more here.",
                confidence=55,
                evidence=native_vs_scan,
            )
        )
    elif native_vs_scan["guess"] == "hybrid":
        findings.append(
            finding(
                fid="pdf-hybrid",
                category="pdf_structure",
                severity="medium",
                title="Mix of native-text pages and image-only pages",
                explanation=(
                    "Some pages have real text objects while others are photographs of pages. "
                    "That often means separately sourced material was merged into one exhibit."
                ),
                why_suspicious="Inconsistent page origins are a structural anomaly, not proof of fraud by themselves.",
                confidence=68,
                evidence=native_vs_scan,
                recommended_action="Review the page-dimension and font tables for the image-only pages.",
            )
        )

    return findings, extras


def _has_javascript(catalog, raw: bytes) -> dict[str, Any] | None:
    hits = {}
    if catalog and isinstance(catalog, DictionaryObject):
        if catalog.get("/OpenAction"):
            hits["open_action"] = True
        names = catalog.get("/Names")
        if isinstance(names, IndirectObject):
            names = names.get_object()
        if isinstance(names, DictionaryObject) and names.get("/JavaScript"):
            hits["names_javascript"] = True
        if catalog.get("/AA"):
            hits["additional_actions"] = True
    if b"/JavaScript" in raw or b"/JS" in raw[: min(len(raw), 2_000_000)]:
        hits["raw_javascript_token"] = True
    return hits or None


def _javascript_finding(javascript: dict[str, Any], origin: dict[str, Any]) -> Finding:
    """Severity depends on whether the exhibit looks scanned vs digitally born.

    Scanned court exhibits often pick up /OpenAction or /JS tokens from e-filing or
    scanner software. That is a container note, not evidence someone altered the
    photographed page. Digitally born PDFs with a real JavaScript name tree are
    treated more seriously.
    """
    scan_heavy = bool(origin.get("scan_heavy"))
    strong_js = bool(javascript.get("names_javascript") or javascript.get("raw_javascript_token"))
    evidence = {
        "details": javascript,
        "scan_heavy": scan_heavy,
        "origin_guess": origin.get("guess"),
        "raster_pages": origin.get("raster_pages"),
    }

    if scan_heavy:
        return finding(
            fid="pdf-javascript",
            category="pdf_structure",
            severity="info",
            title="PDF container has an automatic action or script token",
            explanation=(
                "This file looks like a scan of printed pages (most pages contain embedded "
                "page images) and also has a PDF automatic-action or JavaScript token. "
                "That combination is common after scanning, merging, or e-filing tools. "
                "It is unlikely to change the photographed statement content itself, though "
                "a viewer could still run an action when the file is opened."
            ),
            why_suspicious=(
                "Not primary evidence of alteration on the scanned page. It records that the "
                "PDF wrapper is interactive or tool-processed."
            ),
            confidence=35,
            evidence=evidence,
            recommended_action=(
                "Treat as a container note. If you need to rule out viewer tricks, open the "
                "file in a JavaScript-disabled viewer or print to a flat PDF and compare."
            ),
        )

    if strong_js:
        return finding(
            fid="pdf-javascript",
            category="pdf_structure",
            severity="high",
            title="PDF contains JavaScript or an automatic action",
            explanation=(
                "This digitally born PDF includes JavaScript and/or automatic actions that can "
                "change what a viewer displays versus what is printed or extracted."
            ),
            why_suspicious="Scripts have no place in a typical court exhibit and can hide or swap content.",
            confidence=88,
            evidence=evidence,
            recommended_action="Open the file in a JavaScript-disabled viewer and print to PDF before relying on it.",
        )

    return finding(
        fid="pdf-javascript",
        category="pdf_structure",
        severity="medium",
        title="PDF has an automatic open-action",
        explanation=(
            "The PDF catalog declares an open-action or additional action. That can change "
            "initial view behavior; it is less definitive than an embedded JavaScript name tree."
        ),
        why_suspicious="Automatic actions are uncommon on clean exhibit PDFs and deserve a quick check.",
        confidence=62,
        evidence=evidence,
        recommended_action="Open in a JavaScript-disabled viewer and confirm the visible pages match a printed copy.",
    )


def _page_has_raster_image(page) -> bool:
    """True when the page embeds at least one Image XObject (typical of a scan)."""
    try:
        resources = page.get("/Resources")
        resources = resources.get_object() if isinstance(resources, IndirectObject) else resources
        xobj = resources.get("/XObject") if isinstance(resources, DictionaryObject) else None
        xobj = xobj.get_object() if isinstance(xobj, IndirectObject) else xobj
        if not isinstance(xobj, DictionaryObject):
            return False
        for _name, ref in xobj.items():
            obj = ref.get_object() if isinstance(ref, IndirectObject) else ref
            if isinstance(obj, DictionaryObject) and obj.get("/Subtype") == "/Image":
                return True
    except Exception:
        return False
    return False


def _list_embedded(reader) -> list[str]:
    names: list[str] = []
    try:
        attachments = getattr(reader, "attachments", None) or {}
        names.extend(str(n) for n in attachments.keys())
    except Exception:
        pass
    try:
        if reader.trailer:
            root = reader.trailer.get("/Root")
            root = root.get_object() if isinstance(root, IndirectObject) else root
            names_dict = root.get("/Names") if isinstance(root, DictionaryObject) else None
            names_dict = names_dict.get_object() if isinstance(names_dict, IndirectObject) else names_dict
            ef = names_dict.get("/EmbeddedFiles") if isinstance(names_dict, DictionaryObject) else None
            if ef:
                names.append("(EmbeddedFiles name tree present)")
    except Exception:
        pass
    return names


def _list_layers(catalog) -> list[str]:
    if not catalog or not isinstance(catalog, DictionaryObject):
        return []
    oc = catalog.get("/OCProperties")
    oc = oc.get_object() if isinstance(oc, IndirectObject) else oc
    if not isinstance(oc, DictionaryObject):
        return []
    ocgs = oc.get("/OCGs") or []
    labels = []
    try:
        for item in ocgs:
            obj = item.get_object() if isinstance(item, IndirectObject) else item
            if isinstance(obj, DictionaryObject):
                labels.append(str(obj.get("/Name") or "Unnamed layer"))
    except Exception:
        labels.append("(layers present, names unread)")
    return labels


def _annotations(reader) -> dict[str, Any]:
    summary = defaultdict(int)
    possible_incomplete = False
    for page in reader.pages:
        annots = page.get("/Annots")
        if annots is None:
            continue
        try:
            annots = annots.get_object() if isinstance(annots, IndirectObject) else annots
        except Exception:
            continue
        page_has_black = False
        for annot in annots or []:
            try:
                annot = annot.get_object() if isinstance(annot, IndirectObject) else annot
            except Exception:
                continue
            if not isinstance(annot, DictionaryObject):
                continue
            subtype = str(annot.get("/Subtype") or "")
            summary[subtype] += 1
            if subtype == "/Redact":
                summary["redact_unapplied"] += 1
            if subtype in {"/Square", "/Highlight", "/Ink", "/Stamp"}:
                color = annot.get("/C")
                try:
                    if color and list(color)[:3] == [0, 0, 0]:
                        page_has_black = True
                except Exception:
                    pass
        if page_has_black:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if len(text) > 40:
                possible_incomplete = True
    summary["possible_incomplete_redaction"] = possible_incomplete
    return dict(summary)


def _native_or_image_pdf(reader) -> dict[str, Any]:
    image_pages = 0
    text_pages = 0
    raster_pages = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        has_text = len(text.strip()) > 30
        has_raster = _page_has_raster_image(page)
        if has_raster:
            raster_pages += 1
        has_xobject = False
        try:
            resources = page.get("/Resources")
            resources = resources.get_object() if isinstance(resources, IndirectObject) else resources
            xobj = resources.get("/XObject") if isinstance(resources, DictionaryObject) else None
            has_xobject = xobj is not None
        except Exception:
            pass
        if has_text:
            text_pages += 1
        elif has_xobject:
            image_pages += 1
        else:
            text_pages += 1
    total = max(1, len(reader.pages))
    if image_pages / total >= 0.8 and text_pages == 0:
        guess = "image-only"
    elif image_pages and text_pages:
        guess = "hybrid"
    else:
        guess = "native-text"
    # OCR'd scans still embed page images, so raster share is the reliable scan signal.
    scan_heavy = raster_pages / total >= 0.5 or guess == "image-only"
    return {
        "guess": guess,
        "pages": len(reader.pages),
        "image_pages": image_pages,
        "text_pages": text_pages,
        "raster_pages": raster_pages,
        "scan_heavy": scan_heavy,
    }
