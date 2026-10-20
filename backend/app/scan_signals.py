"""Compact scan signals for the document list.

Read from a stored report. Does not change review status or scoring.
"""

from __future__ import annotations

from typing import Any

# Short labels for the document-row profile strip (not advocacy copy).
_ANALYSIS_TAG_LABELS: dict[str, str] = {
    "date_inconsistency": "Statement dates",
    "historical_impossibility": "Issuer era",
    "math_error": "Statement math",
    "line_self_contradiction": "Line contradiction",
    "template_fabrication": "Template chrome",
    "logo_chrome_mismatch": "Logo chrome",
    "text_overlay": "Text overlay",
    "graphic_manipulation": "Graphic edit",
    "font_mismatch": "Font mismatch",
    "logical_inconsistency": "Logic conflict",
    "spelling_anomaly": "Spelling anomaly",
}

_MODULE_TAG_LABELS: dict[str, str] = {
    "metadata": "Meta",
    "image_forensics": "Image",
    "ocr_text": "OCR",
    "contextual_logic": "Logic",
    "statement_dates": "Dates",
    "statement_body": "Statement",
    "logo_chrome": "Logo",
    "pdf_structure": "PDF",
    "page_anomalies": "Pages",
}

# Prefer forensic interrupts first when trimming the strip.
_ANALYSIS_PRIORITY: tuple[str, ...] = (
    "date_inconsistency",
    "historical_impossibility",
    "math_error",
    "line_self_contradiction",
    "template_fabrication",
    "logo_chrome_mismatch",
    "text_overlay",
    "graphic_manipulation",
)


def _calls(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _analysis_type(item: dict[str, Any]) -> str:
    ev = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw = item.get("analysis_type") or ev.get("analysis_type") or ""
    return str(raw).strip() if raw else ""


def _finding_severity(item: dict[str, Any]) -> str:
    return str(item.get("severity") or "").strip().lower()


def _uniq_strings(values: list[Any], *, limit: int = 6) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _entity_names(entities: Any, *keys: str) -> list[str]:
    if not isinstance(entities, dict):
        return []
    names: list[str] = []
    for key in keys:
        bucket = entities.get(key)
        if isinstance(bucket, list):
            for item in bucket:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    names.append(str(item.get("name") or item.get("display") or item.get("value") or ""))
        elif isinstance(bucket, str):
            names.append(bucket)
    return _uniq_strings(names, limit=8)


def _fact_displays(
    facts: Any,
    *,
    kinds: set[str] | None = None,
    keys: set[str] | None = None,
    limit: int = 6,
) -> list[str]:
    """Pull display values whose fact key or kind matches the allowlists."""
    if not isinstance(facts, list):
        return []
    values: list[str] = []
    for item in facts:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("attribute") or "").strip()
        kind = str(item.get("kind") or "").strip().lower()
        key_hit = bool(keys) and (key in keys or any(token in key for token in keys))
        kind_hit = bool(kinds) and bool(kind) and kind in kinds
        if keys or kinds:
            if not (key_hit or kind_hit):
                continue
        display = item.get("display") or item.get("value")
        if display is None:
            continue
        text = str(display).strip()
        if text:
            values.append(text)
    return _uniq_strings(values, limit=limit)


def _finding_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for item in findings:
        sev = _finding_severity(item)
        if sev in counts:
            counts[sev] += 1
    return {k: v for k, v in counts.items() if v}


def _top_finding_rows(findings: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    ranked = sorted(
        (f for f in findings if isinstance(f, dict) and _finding_severity(f) != "info"),
        key=lambda f: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(_finding_severity(f), 9),
    )
    for item in ranked:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, str] = {
            "title": title[:160],
            "severity": _finding_severity(item) or "medium",
        }
        analysis = _analysis_type(item)
        if analysis:
            row["analysis_type"] = analysis
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _shorten_summary(text: str, *, limit: int = 280) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "…"


def info_summary(
    report: dict[str, Any] | None,
    *,
    data: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> str:
    """One short paragraph: what this file is + what the scan surfaced."""
    if not isinstance(report, dict):
        report = {}
    data = data if isinstance(data, dict) else {}
    exec_sum = report.get("executive_summary")
    if isinstance(exec_sum, str) and exec_sum.strip():
        # Prefer the scored summary; trim filename quotes noise for the list.
        return _shorten_summary(exec_sum.strip(), limit=260)

    parts: list[str] = []
    orgs = data.get("organizations") if isinstance(data.get("organizations"), list) else []
    people = data.get("people") if isinstance(data.get("people"), list) else []
    dates = data.get("dates") if isinstance(data.get("dates"), list) else []
    amounts = data.get("amounts") if isinstance(data.get("amounts"), list) else []
    kind = data.get("kind")
    pages = data.get("pages")

    head_bits: list[str] = []
    if isinstance(kind, str) and kind.strip():
        head_bits.append(kind.replace("_", " "))
    if isinstance(pages, int) and pages > 0:
        head_bits.append(f"{pages} page{'s' if pages != 1 else ''}")
    if orgs:
        head_bits.append(" · ".join(str(o) for o in orgs[:3]))
    if head_bits:
        parts.append(", ".join(head_bits[:2]) if len(head_bits) <= 2 else f"{head_bits[0]}, {head_bits[1]} — {head_bits[2]}")

    detail_bits: list[str] = []
    if people:
        detail_bits.append("Parties: " + ", ".join(str(p) for p in people[:3]))
    if dates:
        detail_bits.append("Dates: " + ", ".join(str(d) for d in dates[:3]))
    if amounts:
        detail_bits.append("Amounts: " + ", ".join(str(a) for a in amounts[:3]))
    if detail_bits:
        parts.append("; ".join(detail_bits))

    tag_list = tags if isinstance(tags, list) else []
    top = data.get("top_findings") if isinstance(data.get("top_findings"), list) else []
    if top:
        titles = [str(t.get("title") or "").strip() for t in top if isinstance(t, dict)]
        titles = [t for t in titles if t][:2]
        if titles:
            parts.append("Flags: " + "; ".join(titles))
    elif tag_list:
        parts.append("Flags: " + ", ".join(str(t) for t in tag_list[:4]))

    if not parts:
        risk = str(report.get("risk_level") or "").strip()
        if risk == "green":
            return "Scan complete — no material interrupt on this file."
        if risk:
            return f"Scan complete — {risk} risk band."
        return ""
    return _shorten_summary(". ".join(parts), limit=280)


def data_profile(
    report: dict[str, Any] | None,
    *,
    snapshot: dict[str, Any] | None = None,
    page_count: int | None = None,
    doc_kind: str | None = None,
) -> dict[str, Any]:
    """Structured profile for the document list (facts / entities / findings)."""
    if not isinstance(report, dict):
        report = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    comparison = report.get("comparison") if isinstance(report.get("comparison"), dict) else {}
    audit = report.get("audit") if isinstance(report.get("audit"), dict) else {}

    entities = comparison.get("entities") if isinstance(comparison.get("entities"), dict) else {}
    if not entities and isinstance(snapshot.get("entities"), dict):
        entities = snapshot["entities"]

    facts = comparison.get("facts") if isinstance(comparison.get("facts"), list) else []
    if not facts and isinstance(snapshot.get("facts"), list):
        facts = snapshot["facts"]

    amounts_src = snapshot.get("amounts") if isinstance(snapshot.get("amounts"), list) else []
    findings = [
        f
        for f in list(report.get("top_findings") or []) + list(report.get("findings") or [])
        if isinstance(f, dict)
    ]

    pages = page_count
    if not isinstance(pages, int) or pages <= 0:
        dims = report.get("page_dimensions") or comparison.get("page_dimensions") or []
        if isinstance(dims, list) and dims:
            pages = len(dims)
        else:
            pages = None

    kind = doc_kind or comparison.get("doc_kind") or snapshot.get("doc_kind")
    kind = str(kind).strip() if isinstance(kind, str) and kind.strip() else None

    orgs = _entity_names(entities, "organizations", "orgs", "issuers", "companies")
    people = _entity_names(entities, "people", "persons", "parties")
    # Facts often carry named amounts/dates when entities are thin.
    date_keys = {
        "first_default_date",
        "foreclosure_date",
        "notice_date",
        "date_of_birth",
        "employment_start",
        "loan_payment_date",
        "statement_date",
        "filed_date",
        "date",
    }
    dates = _fact_displays(facts, keys=date_keys, kinds={"date", "year", "payment_date"}, limit=5)
    money = _fact_displays(
        facts,
        keys={
            "loan_amount",
            "monthly_payment",
            "outstanding_balance",
            "fees",
            "stated_principal",
            "annual_income",
            "balance_on_date",
            "amount",
        },
        kinds={"money", "money_on_date"},
        limit=5,
    )
    if not money and amounts_src:
        money = _uniq_strings(
            [
                (a.get("display") if isinstance(a, dict) else a)
                for a in amounts_src
            ],
            limit=5,
        )

    addresses = _fact_displays(facts, keys={"residence_address", "address"}, kinds={"address"}, limit=3)

    profile: dict[str, Any] = {
        "kind": kind,
        "pages": pages,
        "organizations": orgs,
        "people": people,
        "dates": dates,
        "amounts": money,
        "addresses": addresses,
        "finding_counts": _finding_counts(findings),
        "top_findings": _top_finding_rows(findings, limit=3),
    }
    size = audit.get("file_size_bytes")
    if isinstance(size, int) and size > 0:
        profile["file_size_bytes"] = size
    origin = comparison.get("origin_guess")
    if isinstance(origin, str) and origin.strip():
        profile["origin"] = origin.strip()[:80]
    scan_prof = comparison.get("scan_profile")
    if isinstance(scan_prof, str) and scan_prof.strip():
        profile["scan_profile"] = scan_prof.strip()
    return profile


def profile_tags(report: dict[str, Any] | None, modules: list[dict[str, str]] | None = None) -> list[str]:
    """Compact tags: top analysis types + module errors / AI incomplete."""
    if not isinstance(report, dict):
        report = {}
    tags: list[str] = []
    seen: set[str] = set()

    def add(label: str) -> None:
        text = (label or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        tags.append(text)

    findings = list(report.get("top_findings") or []) + list(report.get("findings") or [])
    by_type: dict[str, str] = {}
    for item in findings:
        if not isinstance(item, dict):
            continue
        analysis = _analysis_type(item)
        if not analysis:
            continue
        sev = _finding_severity(item)
        # Profile strip is interrupt-oriented — skip info-only nuance.
        if sev == "info":
            continue
        by_type[analysis] = sev or "medium"

    ordered = [a for a in _ANALYSIS_PRIORITY if a in by_type]
    ordered.extend(sorted(a for a in by_type if a not in _ANALYSIS_PRIORITY))
    for analysis in ordered:
        label = _ANALYSIS_TAG_LABELS.get(analysis) or analysis.replace("_", " ").title()
        add(label)
        if len(tags) >= 5:
            break

    mod_list = modules if isinstance(modules, list) else []
    if not mod_list:
        for mod in report.get("modules") or []:
            if not isinstance(mod, dict):
                continue
            name = mod.get("name")
            status = mod.get("status")
            if isinstance(name, str) and name and isinstance(status, str) and status:
                mod_list.append({"name": name, "status": status})

    for mod in mod_list:
        if not isinstance(mod, dict):
            continue
        if str(mod.get("status") or "") != "error":
            continue
        name = str(mod.get("name") or "")
        short = _MODULE_TAG_LABELS.get(name) or (name.replace("_", " ").title() if name else "Module")
        # Meta packaging/timestamp crashes are soft-fail: quiet warning chip,
        # not an "error" tag that reads like a blocked check.
        if name == "metadata":
            add("Meta warning")
        else:
            add(f"{short} error")

    if str(report.get("ai_review") or "").lower() == "incomplete":
        add("AI review incomplete")
    elif str(report.get("risk_level") or "").lower() == "incomplete":
        add("Review incomplete")

    return tags[:6]


def scan_signals(
    report: dict[str, Any] | None,
    *,
    snapshot: dict[str, Any] | None = None,
    page_count: int | None = None,
    doc_kind: str | None = None,
) -> dict[str, Any]:
    """Module statuses, profile tags, data profile, and info summary for the list."""
    empty: dict[str, Any] = {
        "scan_modules": [],
        "scan_profile": None,
        "visual_review_required": None,
        "review_mode": None,
        "gemini_calls": None,
        "document_analysis": None,
        "visual_file_sent": None,
        "profile_tags": [],
        "data_profile": {},
        "info_summary": "",
    }
    if not isinstance(report, dict):
        return empty

    modules_out: list[dict[str, str]] = []
    extras: dict[str, Any] = {}
    for mod in report.get("modules") or []:
        if not isinstance(mod, dict):
            continue
        name = mod.get("name")
        status = mod.get("status")
        if isinstance(name, str) and name and isinstance(status, str) and status:
            row: dict[str, str] = {"name": name, "status": status}
            if status == "error":
                detail = ""
                mod_extras = mod.get("extras") if isinstance(mod.get("extras"), dict) else {}
                err = mod_extras.get("error")
                if isinstance(err, str) and err.strip():
                    detail = err.strip()
                elif isinstance(mod.get("summary"), str) and mod["summary"].strip():
                    detail = mod["summary"].strip()
                if detail:
                    row["error"] = detail[:240]
            modules_out.append(row)
        if name == "contextual_logic" and isinstance(mod.get("extras"), dict):
            extras = mod["extras"]

    comparison = report.get("comparison") if isinstance(report.get("comparison"), dict) else {}
    timing = comparison.get("timing") if isinstance(comparison.get("timing"), dict) else {}
    summary = (
        comparison.get("gemini_scan_summary")
        if isinstance(comparison.get("gemini_scan_summary"), dict)
        else {}
    )
    usage = comparison.get("gemini_usage") if isinstance(comparison.get("gemini_usage"), dict) else {}
    ctx_usage = extras.get("gemini_usage") if isinstance(extras.get("gemini_usage"), dict) else {}

    calls = _calls(timing.get("gemini_calls"))
    if calls is None:
        calls = _calls(summary.get("calls"))
    if calls is None:
        calls = _calls(ctx_usage.get("calls"))
    if calls is None:
        calls = _calls(usage.get("calls"))

    if "visual_review_required" in comparison:
        required: bool | None = bool(comparison.get("visual_review_required"))
    elif "visual_review_required" in extras:
        required = bool(extras.get("visual_review_required"))
    else:
        required = None

    mode = comparison.get("review_mode")
    if not isinstance(mode, str) or not mode:
        mode = extras.get("review_mode")
    mode = mode if isinstance(mode, str) and mode else None

    profile = comparison.get("scan_profile")
    profile = profile if isinstance(profile, str) and profile else None

    analysis = extras.get("document_analysis")
    analysis = analysis if isinstance(analysis, str) and analysis else None

    if "document_analysis_result" in extras:
        dar = extras.get("document_analysis_result")
        file_sent: bool | None = bool(isinstance(dar, dict) and dar.get("file_sent"))
    else:
        file_sent = None

    tags = profile_tags(report, modules_out)
    data = data_profile(
        report,
        snapshot=snapshot,
        page_count=page_count,
        doc_kind=doc_kind,
    )
    return {
        "scan_modules": modules_out,
        "scan_profile": profile,
        "visual_review_required": required,
        "review_mode": mode,
        "gemini_calls": calls,
        "document_analysis": analysis,
        "visual_file_sent": file_sent,
        "profile_tags": tags,
        "data_profile": data,
        "info_summary": info_summary(report, data=data, tags=tags),
    }
