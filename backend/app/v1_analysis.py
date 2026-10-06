"""Public v1 analysis API for the thin desktop client (ephemeral jobs).

When matter_id is supplied and the firm has persist_facts, priors are loaded from
fact memory, compare_to_case runs, facts/findings are persisted, and the source
PDF is deleted immediately. Report TTL is skipped for persist firms.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.case_chat import ask_case_from_packet
from app.config import settings
from app.entitlement import require_scan_allowed
from app.ephemeral import delete_job_files, ensure_sweeper, maybe_delete_after_terminal, sweep_stale_jobs
from app.models import UploadResponse
from app.pipeline import run_pipeline
from app.product_events import emit_chat_asked, emit_scan_failed
from app.scan_slots import acquire as acquire_scan_slot
from app.scan_slots import release as release_scan_slot
from app.scan_watchdog import ensure_watchdog
from app.storage import create_job, job_dir, load_progress, load_report, update_job

v1 = APIRouter(prefix="/v1", tags=["v1-analysis"])
logger = logging.getLogger(__name__)

# Short-lived cache: session bearer → entitlement probe (soft-launch browser auth).
_SESSION_ENTITLEMENT_CACHE: dict[str, tuple[float, dict]] = {}
_SESSION_CACHE_TTL_S = 45.0


class CaseChatPacketDoc(BaseModel):
    document_id: str = Field(min_length=1, max_length=80)
    display_name: str | None = None
    original_filename: str | None = None
    doc_kind: str | None = None
    text_excerpt: str = ""
    findings_summary: list[dict] | None = None
    status: str | None = None


class CaseChatPacketBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    focus_document_id: str | None = None
    history: list[dict[str, str]] | None = None
    documents: list[CaseChatPacketDoc] = Field(default_factory=list, max_length=80)


class FactConflictPacketDoc(BaseModel):
    document_id: str = Field(min_length=1, max_length=80)
    display_name: str | None = None
    original_filename: str | None = None
    doc_kind: str | None = None
    text_excerpt: str = ""
    facts: list[dict] | None = None


class FactConflictPacketBody(BaseModel):
    documents: list[FactConflictPacketDoc] = Field(default_factory=list, max_length=80)


def _probe_control_plane_session(token: str) -> dict | None:
    """Return entitlement dict when the control-plane session is active; else None."""
    control = (settings.cloud_control_url or "").strip().rstrip("/")
    if not control or not token:
        return None
    now = time.time()
    cached = _SESSION_ENTITLEMENT_CACHE.get(token)
    if cached and cached[0] > now:
        return cached[1]
    req = urllib.request.Request(
        f"{control}/v1/entitlement",
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        logger.info("Control-plane session probe failed: %s", exc)
        return None
    if not isinstance(data, dict) or not data.get("active"):
        return None
    _SESSION_ENTITLEMENT_CACHE[token] = (now + _SESSION_CACHE_TTL_S, data)
    # Opportunistic prune
    if len(_SESSION_ENTITLEMENT_CACHE) > 256:
        stale = [k for k, (exp, _) in _SESSION_ENTITLEMENT_CACHE.items() if exp <= now]
        for k in stale:
            _SESSION_ENTITLEMENT_CACHE.pop(k, None)
    return data


def _require_analysis_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    keys = settings.analysis_api_key_set()
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    api_key = (x_api_key or "").strip() or None
    # Prefer explicit API key header; bearer may also be an ops key.
    token_as_key = api_key or (bearer if bearer and bearer in keys else None)

    mode = settings.resolve_app_mode()
    # Hosted analysis: ops API key OR active control-plane session (browser soft-launch).
    if mode == "analysis":
        if token_as_key and token_as_key in keys:
            return {"auth": "api_key"}
        if bearer:
            remote = _probe_control_plane_session(bearer)
            if remote:
                return {"auth": "session", "email": remote.get("email"), "plan": remote.get("plan")}
        raise HTTPException(
            401,
            "Valid analysis API key or active subscription session required.",
        )

    if keys:
        if not token_as_key or token_as_key not in keys:
            # Allow session bearer even when keys are configured (desktop hybrid).
            if bearer:
                remote = _probe_control_plane_session(bearer)
                if remote:
                    return {"auth": "session", "email": remote.get("email"), "plan": remote.get("plan")}
            raise HTTPException(401, "Valid analysis API key required.")
        return {"auth": "api_key"}

    # Local vault: fall back to product entitlement (cloud/license/open).
    try:
        return require_scan_allowed()
    except PermissionError as exc:
        raise HTTPException(402, str(exc)) from exc


def resolve_firm_context(auth: dict) -> dict:
    """Map session email (or API-key ops) → firm_id / user_id. 1:1 firm per account."""
    from app.fact_memory.store import get_store

    store = get_store()
    email = (auth.get("email") or "").strip().lower()
    if email:
        return store.ensure_firm_for_email(email)
    # API-key / local open mode: shared ops firm (still firm-scoped rows).
    return store.ensure_firm_for_email("ops@casesifter.local", name="Ops / API key")


def _should_persist(auth: dict, firm_ctx: dict | None) -> bool:
    if not settings.persist_facts_enabled():
        return False
    if firm_ctx is None:
        return False
    return bool(firm_ctx.get("persist_facts", True))


@v1.post("/jobs", response_model=UploadResponse)
async def create_ephemeral_job(
    file: UploadFile = File(...),
    submitted_by: str = Form(default="desktop-client"),
    display_name: str | None = Form(default=None),
    doc_kind: str | None = Form(default=None),
    peer_snapshots: str | None = Form(default=None),
    matter_id: str | None = Form(default=None),
    case_id: str | None = Form(default=None),  # alias for matter_id
    filed_date: str | None = Form(default=None),
    _auth: dict = Depends(_require_analysis_auth),
):
    ensure_sweeper()
    ensure_watchdog()
    if not file.filename:
        raise HTTPException(400, "A file name is required.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(400, f"Unsupported file type '{suffix}'.")

    peers: list[dict] = []
    if peer_snapshots and peer_snapshots.strip():
        try:
            raw = json.loads(peer_snapshots)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "peer_snapshots must be JSON.") from exc
        if not isinstance(raw, list):
            raise HTTPException(400, "peer_snapshots must be a JSON list.")
        peers = [row for row in raw[:80] if isinstance(row, dict)]

    resolved_matter = (matter_id or case_id or "").strip() or None
    firm_ctx = None
    persist = False
    if resolved_matter:
        firm_ctx = resolve_firm_context(_auth)
        persist = _should_persist(_auth, firm_ctx)
        from app.fact_memory.store import get_store

        store = get_store()
        matter = store.get_matter(firm_ctx["firm_id"], resolved_matter)
        if not matter:
            raise HTTPException(404, "Matter not found for this account.")
        if persist:
            priors = store.list_prior_snapshots(firm_ctx["firm_id"], resolved_matter)
            # Prefer matter priors over client peer_snapshots when both present.
            if priors:
                peers = priors

    job_id = uuid.uuid4().hex
    dest_dir = job_dir(job_id)
    safe_name = Path(file.filename).name
    dest = dest_dir / safe_name

    size = 0
    max_bytes = settings.max_file_mb * 1024 * 1024
    with dest.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                handle.close()
                delete_job_files(job_id)
                raise HTTPException(400, f"File exceeds {settings.max_file_mb} MB limit.")
            handle.write(chunk)

    create_job(job_id, safe_name)
    update_job(
        job_id,
        message="Queued for analysis."
        + (" Matter fact memory enabled." if persist else ""),
    )
    if persist:
        # Marker so sweeper retains report (facts live in DB; PDF still deleted).
        (dest_dir / "persist.flag").write_text(resolved_matter or "", encoding="utf-8")

    thread = threading.Thread(
        target=_run_ephemeral,
        args=(
            job_id,
            dest,
            submitted_by,
            display_name,
            doc_kind,
            peers,
            resolved_matter,
            firm_ctx,
            filed_date,
            persist,
        ),
        daemon=True,
        name=f"ephemeral-{job_id[:8]}",
    )
    thread.start()
    return UploadResponse(job_id=job_id, filename=safe_name, status="queued")


def _run_ephemeral(
    job_id: str,
    dest: Path,
    submitted_by: str,
    display_name: str | None,
    doc_kind: str | None,
    prior_snapshots: list[dict] | None = None,
    matter_id: str | None = None,
    firm_ctx: dict | None = None,
    filed_date: str | None = None,
    persist: bool = False,
) -> None:
    if not acquire_scan_slot(job_id, timeout=600):
        emit_scan_failed(
            job_id=job_id,
            stage="queue",
            error_class="timeout",
            error_message="Scan queue timed out.",
        )
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message="Scan queue timed out.",
            error="Another scan held the queue for too long.",
        )
        maybe_delete_after_terminal(job_id, retain_report=persist)
        return
    try:
        report = run_pipeline(
            job_id,
            dest,
            submitted_by=submitted_by,
            display_name=display_name,
            doc_kind=doc_kind,
            prior_snapshots=prior_snapshots or None,
        )
        if persist and matter_id and firm_ctx and report is not None:
            _persist_pipeline_result(
                job_id=job_id,
                source=dest,
                report=report,
                matter_id=matter_id,
                firm_ctx=firm_ctx,
                display_name=display_name,
                doc_kind=doc_kind,
                filed_date=filed_date,
            )
    except Exception as exc:
        emit_scan_failed(job_id=job_id, stage="failed", error_message=str(exc))
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message="Analysis failed.",
            error=str(exc),
        )
    finally:
        release_scan_slot(job_id)
        maybe_delete_after_terminal(job_id, retain_report=persist)


def _persist_pipeline_result(
    *,
    job_id: str,
    source: Path,
    report,
    matter_id: str,
    firm_ctx: dict,
    display_name: str | None,
    doc_kind: str | None,
    filed_date: str | None,
) -> None:
    from app.fact_memory.store import get_store
    from app.ingestion import sha256_file

    store = get_store()
    digest = ""
    if source.exists():
        digest = sha256_file(source)
    elif report.audit and report.audit.sha256:
        digest = report.audit.sha256
    comparison = report.comparison or {}
    facts = comparison.get("facts") or []
    entities = comparison.get("entities") or {}
    for mod in report.modules or []:
        extras = getattr(mod, "extras", None) or {}
        cc = extras.get("case_consistency") if isinstance(extras, dict) else None
        if isinstance(cc, dict):
            facts = facts or cc.get("facts") or []
            break
    page_count = len(report.page_dimensions or []) or None
    finding_dicts = [f.model_dump(mode="json") for f in (report.findings or [])]
    report_dict = report.model_dump(mode="json")
    try:
        result = store.persist_scan(
            firm_id=firm_ctx["firm_id"],
            matter_id=matter_id,
            job_id=job_id,
            sha256=digest,
            original_filename=source.name,
            display_name=display_name or source.name,
            doc_type=doc_kind,
            filed_date=filed_date,
            page_count=page_count,
            entities=entities if isinstance(entities, dict) else {},
            facts=facts if isinstance(facts, list) else [],
            findings=finding_dicts,
            text_fingerprint=comparison.get("text_fingerprint"),
            text_excerpt=(comparison.get("text_excerpt") or "")[:24000],
            page_offsets=comparison.get("page_offsets"),
            amounts=comparison.get("amounts"),
            extractor_version=settings.app_version,
            user_id=firm_ctx.get("user_id"),
            report=report_dict,
        )
        logger.info(
            "Persisted facts for matter=%s job=%s persisted=%s deduped=%s",
            matter_id,
            job_id,
            result.get("persisted"),
            result.get("deduped"),
        )
    except Exception:
        logger.exception("Failed to persist fact memory for job %s", job_id)


@v1.get("/jobs/{job_id}")
def get_ephemeral_job(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    progress = load_progress(job_id)
    if progress is None:
        raise HTTPException(404, "Unknown or expired job.")
    payload = progress.model_dump(mode="json")
    payload["ephemeral"] = True
    persist_flag = job_dir(job_id) / "persist.flag"
    if persist_flag.exists():
        payload["ephemeral"] = False
        payload["persist_facts"] = True
        try:
            payload["matter_id"] = persist_flag.read_text(encoding="utf-8").strip() or None
        except OSError:
            payload["matter_id"] = None
    if progress.status == "complete":
        report = load_report(job_id)
        payload["report"] = report.model_dump(mode="json") if report else None
    else:
        payload["report"] = None
    return payload


@v1.get("/jobs/{job_id}/report")
def get_ephemeral_report(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    report = load_report(job_id)
    if report is None:
        progress = load_progress(job_id)
        if progress is None:
            raise HTTPException(404, "Unknown or expired job.")
        raise HTTPException(409, "Report is not ready yet.")
    return JSONResponse(report.model_dump(mode="json"))


@v1.delete("/jobs/{job_id}")
def purge_ephemeral_job(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    ok = delete_job_files(job_id)
    if not ok:
        raise HTTPException(404, "Unknown or already purged job.")
    return {"deleted": job_id}


@v1.post("/case-chat")
def ephemeral_case_chat(
    body: CaseChatPacketBody,
    _auth: dict = Depends(_require_analysis_auth),
):
    """Answer from client-supplied excerpts only — request-scoped, no document vault."""
    docs = [item.model_dump() for item in body.documents]
    total = sum(len(d.get("text_excerpt") or "") for d in docs)
    if total > 600_000:
        raise HTTPException(413, "Excerpt packet too large. Rescan fewer documents or shorten excerpts.")
    try:
        result = ask_case_from_packet(
            body.question,
            docs,
            history=body.history,
            focus_document_id=body.focus_document_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    citations = result.get("citations") or []
    emit_chat_asked(
        used_model=bool(result.get("used_model")),
        hit_count=len(citations),
        question_len=len(body.question or ""),
    )
    return {
        "answer": result.get("answer"),
        "citations": citations,
        "used_model": bool(result.get("used_model")),
        "ephemeral": True,
    }


@v1.post("/fact-conflicts")
def ephemeral_fact_conflicts(
    body: FactConflictPacketBody,
    _auth: dict = Depends(_require_analysis_auth),
):
    """Case-level labeled-fact conflicts from a desktop report packet (no vault)."""
    from app.case_consistency import fact_conflicts_from_packet

    docs = [item.model_dump() for item in body.documents]
    total = sum(len(d.get("text_excerpt") or "") for d in docs)
    if total > 600_000:
        raise HTTPException(413, "Excerpt packet too large. Rescan fewer documents or shorten excerpts.")
    if len(docs) < 2:
        return {"fact_conflicts": [], "ephemeral": True}
    conflicts = fact_conflicts_from_packet(docs)
    material = [row for row in conflicts if row.get("severity") in {"high", "medium"}]
    return {"fact_conflicts": material, "ephemeral": True}


@v1.post("/sweep")
def force_sweep(_auth: dict = Depends(_require_analysis_auth)):
    return {"removed": sweep_stale_jobs()}
