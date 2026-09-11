from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from typing import Any

import piexif
from dateutil import parser as dateparser
from pypdf.generic import IndirectObject

from app.engines import finding, timed_module
from app.ingestion import open_pdf_reader
from app.models import Finding, ModuleResult, TimelineEvent

NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

EDITING_SOFTWARE = (
    "photoshop",
    "gimp",
    "photopea",
    "affinity",
    "pixelmator",
    "paint.net",
    "corel",
    "snapseed",
    "lightroom",
)


def analyze_metadata(path: Path) -> ModuleResult:
    return timed_module("metadata", lambda: _run(path))


def _run(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    events: list[TimelineEvent] = []
    meta: dict[str, Any] = {"filename": path.name, "extension": path.suffix.lower()}

    stat = path.stat()
    fs_created = _from_timestamp(getattr(stat, "st_ctime", stat.st_mtime))
    fs_modified = _from_timestamp(stat.st_mtime)
    meta["filesystem"] = {
        "created": fs_created,
        "modified": fs_modified,
        "size_bytes": stat.st_size,
    }
    events.append(TimelineEvent(label="Filesystem created", timestamp=fs_created, source="filesystem"))
    events.append(TimelineEvent(label="Filesystem modified", timestamp=fs_modified, source="filesystem"))

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdf_meta, pdf_findings, pdf_events = _pdf_metadata(path)
        meta["document"] = pdf_meta
        findings.extend(pdf_findings)
        events.extend(pdf_events)
    elif suffix == ".docx":
        office_meta, office_findings, office_events = _office_core_props(path, "word")
        meta["document"] = office_meta
        findings.extend(office_findings)
        events.extend(office_events)
    elif suffix == ".xlsx":
        office_meta, office_findings, office_events = _office_core_props(path, "xl")
        meta["document"] = office_meta
        findings.extend(office_findings)
        events.extend(office_events)
    elif suffix in {".jpg", ".jpeg", ".png", ".gif"}:
        img_meta, img_findings, img_events = _image_metadata(path)
        meta["document"] = img_meta
        findings.extend(img_findings)
        events.extend(img_events)

    _compare_timestamps(meta, findings, events)
    extras = {"metadata": meta, "timeline": [e.model_dump() for e in events]}
    return findings, extras


def _from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _pdf_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    text = str(value)
    match = re.search(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", text)
    if not match:
        try:
            return dateparser.parse(text).isoformat()
        except Exception:
            return text
    parts = [int(p) if p else 0 for p in match.groups()]
    year, month, day, hour, minute, second = parts
    month = month or 1
    day = day or 1
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).isoformat()
    except ValueError:
        return text


def _deref(value: Any) -> Any:
    if isinstance(value, IndirectObject):
        try:
            return value.get_object()
        except Exception:
            return value
    return value


def _pdf_metadata(path: Path) -> tuple[dict[str, Any], list[Finding], list[TimelineEvent]]:
    findings: list[Finding] = []
    events: list[TimelineEvent] = []
    reader = open_pdf_reader(path)
    info = reader.metadata or {}
    meta = {
        "title": str(info.title) if info and info.title else None,
        "author": str(info.author) if info and info.author else None,
        "creator": str(info.creator) if info and info.creator else None,
        "producer": str(info.producer) if info and info.producer else None,
        "subject": str(info.subject) if info and info.subject else None,
        "created": _pdf_date(info.creation_date) if info else None,
        "modified": _pdf_date(info.modification_date) if info else None,
        "page_count": len(reader.pages),
        "pdf_version": getattr(reader, "pdf_header", None),
        "is_encrypted": bool(reader.is_encrypted),
    }

    if not any([meta["author"], meta["creator"], meta["producer"], meta["created"]]):
        findings.append(
            finding(
                fid="md-stripped",
                category="metadata",
                severity="low",
                title="Document metadata is missing or stripped",
                explanation=(
                    "This PDF has no author, creator, producer, or creation date. "
                    "Some legitimate scanners omit metadata, but wholesale stripping "
                    "can also hide editing history."
                ),
                why_suspicious="Absence of standard PDF descriptive metadata can indicate sanitization.",
                confidence=45,
                recommended_action="Ask the submitting party how the PDF was created and whether metadata was removed.",
            )
        )
    else:
        if meta["created"]:
            events.append(
                TimelineEvent(
                    label="PDF created",
                    timestamp=meta["created"],
                    source="pdf.info",
                    note=meta.get("creator") or meta.get("producer"),
                )
            )
        if meta["modified"]:
            events.append(
                TimelineEvent(
                    label="PDF modified",
                    timestamp=meta["modified"],
                    source="pdf.info",
                    note=meta.get("producer"),
                )
            )

    software = " ".join(filter(None, [meta.get("creator"), meta.get("producer")])).lower()
    if any(name in software for name in EDITING_SOFTWARE):
        findings.append(
            finding(
                fid="md-editor-software",
                category="metadata",
                severity="medium",
                title="PDF was produced by image-editing software",
                explanation=(
                    f"Creator/Producer identifies '{meta.get('creator') or ''} {meta.get('producer') or ''}'. "
                    "Image editors are commonly used to alter scans and photographs before exporting a PDF."
                ),
                why_suspicious="Editing software in the producer field is a classic sign the file is not a native original.",
                confidence=72,
                evidence={"creator": meta.get("creator"), "producer": meta.get("producer")},
                recommended_action="Compare this file to any earlier native version and inspect image-forensics heatmaps.",
            )
        )

    if meta["created"] and meta["modified"]:
        created = _try_parse(meta["created"])
        modified = _try_parse(meta["modified"])
        if created and modified and modified < created:
            findings.append(
                finding(
                    fid="md-date-order",
                    category="metadata",
                    severity="high",
                    title="Modification date is earlier than creation date",
                    explanation=(
                        f"Creation is recorded as {meta['created']} but modification is {meta['modified']}. "
                        "A clock cannot move backwards without metadata editing or a misconfigured system."
                    ),
                    why_suspicious="Impossible timestamp order almost always means the metadata was altered.",
                    confidence=88,
                    evidence={"created": meta["created"], "modified": meta["modified"]},
                    recommended_action="Treat timestamps as unreliable; request original source files.",
                )
            )

    now = datetime.now(timezone.utc)
    for label, raw in (("created", meta["created"]), ("modified", meta["modified"])):
        parsed = _try_parse(raw) if raw else None
        if parsed and parsed > now:
            findings.append(
                finding(
                    fid=f"md-future-{label}",
                    category="metadata",
                    severity="high",
                    title=f"PDF {label} date is in the future",
                    explanation=f"The {label} timestamp ({raw}) is after the scan time.",
                    why_suspicious="Future dates indicate clock spoofing or hand-edited metadata.",
                    confidence=90,
                    evidence={label: raw},
                )
            )

    return meta, findings, events


def _office_core_props(
    path: Path, kind: str
) -> tuple[dict[str, Any], list[Finding], list[TimelineEvent]]:
    findings: list[Finding] = []
    events: list[TimelineEvent] = []
    meta: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "docProps/core.xml" not in names:
                findings.append(
                    finding(
                        fid="md-office-core-missing",
                        category="metadata",
                        severity="medium",
                        title="Office core properties are missing",
                        explanation="The package has no docProps/core.xml. That file normally stores author and dates.",
                        why_suspicious="Core properties are sometimes deleted to hide authorship or revision history.",
                        confidence=70,
                        recommended_action="Request the original working file rather than an exported copy.",
                    )
                )
                return meta, findings, events

            root = ET.fromstring(archive.read("docProps/core.xml"))
            meta = {
                "title": _xml_text(root, "dc:title"),
                "creator": _xml_text(root, "dc:creator"),
                "last_modified_by": _xml_text(root, "cp:lastModifiedBy"),
                "created": _xml_text(root, "dcterms:created"),
                "modified": _xml_text(root, "dcterms:modified"),
                "revision": _xml_text(root, "cp:revision"),
                "subject": _xml_text(root, "dc:subject"),
            }

            app_meta = {}
            if "docProps/app.xml" in names:
                app_root = ET.fromstring(archive.read("docProps/app.xml"))
                app_meta = {
                    "application": _first_tag_text(app_root, "Application"),
                    "app_version": _first_tag_text(app_root, "AppVersion"),
                    "company": _first_tag_text(app_root, "Company"),
                    "total_time": _first_tag_text(app_root, "TotalTime"),
                }
                meta.update(app_meta)

            if kind == "word":
                meta["has_revisions"] = any(n.startswith("word/") and n.endswith("people.xml") for n in names) or (
                    "word/document.xml" in names and b"<w:del " in archive.read("word/document.xml")
                )
                if "word/document.xml" in names:
                    doc_xml = archive.read("word/document.xml")
                    meta["tracked_deletions"] = doc_xml.count(b"<w:del ")
                    meta["tracked_insertions"] = doc_xml.count(b"<w:ins ")
                    if meta["tracked_deletions"] or meta["tracked_insertions"]:
                        findings.append(
                            finding(
                                fid="md-tracked-changes",
                                category="metadata",
                                severity="medium",
                                title="Word document still contains tracked changes",
                                explanation=(
                                    f"Found {meta['tracked_insertions']} insertion(s) and "
                                    f"{meta['tracked_deletions']} deletion(s). Tracked changes show the document "
                                    "was edited after an earlier draft."
                                ),
                                why_suspicious="Unaccepted revisions mean the visible text may not match an earlier ‘final’ version.",
                                confidence=80,
                                evidence={
                                    "insertions": meta["tracked_insertions"],
                                    "deletions": meta["tracked_deletions"],
                                },
                                recommended_action="Review tracked changes in Microsoft Word before relying on the visible text.",
                            )
                        )

            if not meta.get("creator") and not meta.get("created"):
                findings.append(
                    finding(
                        fid="md-office-empty",
                        category="metadata",
                        severity="low",
                        title="Author and creation timestamps are empty",
                        explanation="Core properties exist but author and dates were cleared.",
                        why_suspicious="Empty authorship fields can result from metadata scrubbing tools.",
                        confidence=50,
                    )
                )

            if meta.get("created"):
                events.append(
                    TimelineEvent(
                        label="Office document created",
                        timestamp=_normalize_date(meta["created"]),
                        source="docProps/core.xml",
                        note=meta.get("creator"),
                    )
                )
            if meta.get("modified"):
                events.append(
                    TimelineEvent(
                        label="Office document modified",
                        timestamp=_normalize_date(meta["modified"]),
                        source="docProps/core.xml",
                        note=meta.get("last_modified_by"),
                    )
                )

            created = _try_parse(meta.get("created"))
            modified = _try_parse(meta.get("modified"))
            if created and modified and modified < created:
                findings.append(
                    finding(
                        fid="md-office-date-order",
                        category="metadata",
                        severity="high",
                        title="Office modification date precedes creation date",
                        explanation=f"Created {meta.get('created')}, modified {meta.get('modified')}.",
                        why_suspicious="Impossible date order indicates edited or transplanted metadata.",
                        confidence=86,
                    )
                )

            try:
                revision = int(meta["revision"]) if meta.get("revision") else None
            except ValueError:
                revision = None
            if revision is not None and revision >= 25:
                findings.append(
                    finding(
                        fid="md-high-revision",
                        category="metadata",
                        severity="info",
                        title="High revision count",
                        explanation=f"The file reports revision {revision}, meaning it was saved many times.",
                        why_suspicious="A high save count is not fraud by itself, but it shows a long edit history.",
                        confidence=40,
                        evidence={"revision": revision},
                    )
                )
    except zipfile.BadZipFile:
        findings.append(
            finding(
                fid="md-office-corrupt",
                category="metadata",
                severity="high",
                title="Office file is not a valid ZIP package",
                explanation="DOCX/XLSX files are ZIP archives. This file could not be opened as one.",
                why_suspicious="A rebuilt or renamed file may have a valid extension but a broken internal structure.",
                confidence=78,
            )
        )
    return meta, findings, events


def _image_metadata(path: Path) -> tuple[dict[str, Any], list[Finding], list[TimelineEvent]]:
    from PIL import Image

    findings: list[Finding] = []
    events: list[TimelineEvent] = []
    meta: dict[str, Any] = {"format": path.suffix.lower()}
    with Image.open(path) as img:
        meta["size"] = {"width": img.width, "height": img.height}
        meta["mode"] = img.mode
        meta["dpi"] = img.info.get("dpi")
        meta["software"] = img.info.get("software") or img.info.get("Software")

    if path.suffix.lower() in {".jpg", ".jpeg"}:
        try:
            exif = piexif.load(str(path))
            readable = {}
            for ifd_name in ("0th", "Exif", "GPS"):
                ifd = exif.get(ifd_name) or {}
                for tag, value in ifd.items():
                    name = piexif.TAGS.get(ifd_name, {}).get(tag, {}).get("name", str(tag))
                    readable[name] = _decode_exif_value(value)
            meta["exif"] = readable
            dt = (
                readable.get("DateTimeOriginal")
                or readable.get("DateTimeDigitized")
                or readable.get("DateTime")
            )
            if dt:
                iso = _normalize_date(str(dt).replace(":", "-", 2))
                events.append(
                    TimelineEvent(label="EXIF original capture", timestamp=iso, source="exif")
                )
                meta["created"] = iso
            software = str(readable.get("Software") or meta.get("software") or "").lower()
            if any(name in software for name in EDITING_SOFTWARE):
                findings.append(
                    finding(
                        fid="md-exif-editor",
                        category="metadata",
                        severity="medium",
                        title="EXIF software tag names an image editor",
                        explanation=f"Software field is '{readable.get('Software') or meta.get('software')}'.",
                        why_suspicious="Editors rewrite pixels and often leave a software stamp.",
                        confidence=74,
                        evidence={"software": readable.get("Software")},
                    )
                )
            if not readable:
                findings.append(
                    finding(
                        fid="md-exif-empty",
                        category="metadata",
                        severity="info",
                        title="JPEG has no EXIF metadata",
                        explanation=(
                            "Many phones and cameras embed EXIF data. An empty EXIF block can mean the "
                            "image was exported, screenshot, or stripped. It is common and not proof of fraud."
                        ),
                        why_suspicious="Metadata stripping removes a useful authenticity trail.",
                        confidence=35,
                    )
                )
        except Exception:
            meta["exif"] = None
            findings.append(
                finding(
                    fid="md-exif-unreadable",
                    category="metadata",
                    severity="low",
                    title="EXIF data could not be parsed",
                    explanation="The JPEG header did not contain a readable EXIF segment.",
                    why_suspicious="Corrupt or truncated EXIF can follow export from an editor.",
                    confidence=40,
                )
            )
    return meta, findings, events


def _compare_timestamps(meta: dict[str, Any], findings: list[Finding], events: list[TimelineEvent]) -> None:
    fs = meta.get("filesystem") or {}
    doc = meta.get("document") or {}
    created = _try_parse(doc.get("created"))
    modified = _try_parse(doc.get("modified"))
    fs_modified = _try_parse(fs.get("modified"))

    if created and modified:
        delta = abs((modified - created).total_seconds())
        if 0 < delta < 30:
            events.append(
                TimelineEvent(
                    label="Near-simultaneous create/modify",
                    timestamp=doc.get("modified"),
                    source="comparison",
                    note="Create and modify times are within 30 seconds (typical of a clean export).",
                )
            )
        elif delta > 365 * 24 * 3600:
            findings.append(
                finding(
                    fid="md-year-gap",
                    category="metadata",
                    severity="medium",
                    title="More than a year between creation and modification",
                    explanation=(
                        f"Created {doc.get('created')} and last modified {doc.get('modified')}. "
                        "A long gap can be normal (a file re-saved years later) or can mean an old "
                        "document was altered after it was treated as final."
                    ),
                    why_suspicious="Large create/modify gaps are a prompt to ask whether a ‘final’ copy was later changed.",
                    confidence=58,
                    evidence={"created": doc.get("created"), "modified": doc.get("modified")},
                    recommended_action="Confirm whether later edits were authorized and request version history.",
                )
            )

    if created and fs_modified and fs_modified < created:
        findings.append(
            finding(
                fid="md-fs-before-create",
                category="metadata",
                severity="low",
                title="Filesystem timestamp is earlier than embedded creation date",
                explanation=(
                    "The operating-system file date is earlier than the date stored inside the document. "
                    "Copying files between systems often causes this; it can also follow metadata injection."
                ),
                why_suspicious="Internal and filesystem clocks disagree.",
                confidence=42,
            )
        )


def _xml_text(root: ET.Element, qname: str) -> str | None:
    node = root.find(qname, NS)
    if node is None or node.text is None:
        return None
    text = node.text.strip()
    return text or None


def _first_tag_text(root: ET.Element, local: str) -> str | None:
    for node in root.iter():
        if node.tag.endswith(local) and node.text:
            return node.text.strip()
    return None


def _decode_exif_value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace").strip("\x00")
        except Exception:
            return repr(value)
    if isinstance(value, tuple) and value and isinstance(value[0], bytes):
        return _decode_exif_value(value[0])
    return value


def _try_parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = dateparser.parse(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _normalize_date(value: str | None) -> str | None:
    parsed = _try_parse(value)
    return parsed.isoformat() if parsed else value
