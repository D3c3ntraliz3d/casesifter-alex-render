"""Public v1 analysis API for the thin desktop client (ephemeral jobs).

When matter_id is supplied and the firm has persist_facts, priors are loaded from
fact memory, compare_to_case runs, facts/findings are persisted, and the source
PDF is deleted immediately. Report TTL is skipped for persist firms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.case_chat import ask_case_from_packet
from app.config import settings
from app.entitlement import require_scan_allowed
from app.ephemeral import (
    delete_job_files,
    ensure_sweeper,
    sweep_stale_jobs,
)
from app.models import UploadResponse
from app.product_events import emit_chat_asked
from app.scan_executor import ensure_scan_executor, submit_ephemeral_job
from app.scan_watchdog import ensure_watchdog
from app.storage import create_job, job_dir, load_progress, load_report, update_job

v1 = APIRouter(prefix="/v1", tags=["v1-analysis"])
logger = logging.getLogger(__name__)

# Short-lived cache: session bearer → entitlement probe (soft-launch browser auth).
_SESSION_ENTITLEMENT_CACHE: dict[str, tuple[float, dict]] = {}
_SESSION_CACHE_TTL_S = 45.0


def _resolve_upload_labels(
    filename: str,
    display_name: str | None,
    doc_kind: str | None,
    text_sample: str = "",
) -> tuple[str, str | None]:
    """Mirror local vault upload labeling for hosted /v1/jobs + matter persist.

    Hosted clients often echo the original filename as display_name and omit
    doc_kind. Without this, EXHIBIT(S) packets persist as Unknown.
    """
    from app.auto_title import looks_like_exhibit_filename, propose_document_title

    safe = Path(filename or "").name
    try:
        proposed_name, proposed_kind = propose_document_title(safe, text_sample or "")
    except Exception:
        proposed_name, proposed_kind = Path(safe).stem.replace("_", " "), None

    user_name = (display_name or "").strip()
    # Hosted uploadCaseDocument sends file.name as display_name — treat as unset.
    if not user_name or user_name == safe or user_name == (filename or "").strip():
        final_name = proposed_name
    else:
        final_name = user_name

    user_kind = (doc_kind or "").strip()
    if looks_like_exhibit_filename(safe) or looks_like_exhibit_filename(final_name):
        final_kind = "exhibit"
        if (
            (not user_name or user_name == safe or user_name == (filename or "").strip())
            and proposed_name
        ):
            final_name = proposed_name
    elif user_kind:
        final_kind = user_kind
    elif proposed_kind:
        # Keep court-filing kinds (affidavit, affirmation, proof_of_service, …).
        # Nulling COURT_FILING_KINDS left hosted Affirmation/Affidavit rows as Unknown.
        final_kind = proposed_kind.strip().lower()
    else:
        final_kind = None
    return final_name, final_kind


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


class ControlPlaneUnavailable(Exception):
    """Control plane unreachable while refreshing entitlement."""


def _probe_control_plane_session(token: str) -> dict | None:
    """Fetch entitlement from control plane. Raises ControlPlaneUnavailable on network failure."""
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
        logger.info("Control-plane entitlement probe failed: %s", exc)
        raise ControlPlaneUnavailable(str(exc)) from exc
    if not isinstance(data, dict):
        return None
    _SESSION_ENTITLEMENT_CACHE[token] = (now + _SESSION_CACHE_TTL_S, data)
    if len(_SESSION_ENTITLEMENT_CACHE) > 256:
        stale = [k for k, (exp, _) in _SESSION_ENTITLEMENT_CACHE.items() if exp <= now]
        for k in stale:
            _SESSION_ENTITLEMENT_CACHE.pop(k, None)
    return data


def _require_analysis_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    """Authenticate only — does not require an active paid/trial entitlement."""
    keys = settings.analysis_api_key_set()
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    api_key = (x_api_key or "").strip() or None
    token_as_key = api_key or (bearer if bearer and bearer in keys else None)

    mode = settings.resolve_app_mode()
    if mode == "analysis":
        if token_as_key and token_as_key in keys:
            return {"auth": "api_key", "authenticated": True, "entitled": True}
        if bearer:
            from app.session_auth import (
                SessionLookupUnavailable,
                session_secret,
                verify_bearer_locally,
            )

            # Fail closed: no SESSION_SECRET → reject all browser sessions.
            # Never fall back to a default secret or probe-only identity.
            if not session_secret():
                raise HTTPException(
                    401,
                    "Valid analysis API key or active subscription session required.",
                )
            try:
                local = verify_bearer_locally(bearer)
            except SessionLookupUnavailable:
                raise HTTPException(
                    503,
                    "Session store temporarily unavailable. Try again shortly.",
                ) from None
            if not local:
                raise HTTPException(
                    401,
                    "Valid analysis API key or active subscription session required.",
                )
            remote = None
            entitlement_unavailable = False
            try:
                remote = _probe_control_plane_session(bearer)
            except ControlPlaneUnavailable:
                cached = _SESSION_ENTITLEMENT_CACHE.get(bearer)
                if cached:
                    remote = cached[1]
                else:
                    entitlement_unavailable = True
            email = local.get("email") or (remote or {}).get("email")
            entitled = bool((remote or {}).get("active")) if remote is not None else False
            if remote is None and entitlement_unavailable:
                entitled = False
            return {
                "auth": "session",
                "authenticated": True,
                "entitled": entitled,
                "email": email,
                "user_id": local.get("user_id") or (remote or {}).get("user_id"),
                "plan": (remote or {}).get("plan"),
                "bearer": bearer,
                "entitlement_unavailable": entitlement_unavailable and remote is None,
            }
        raise HTTPException(
            401,
            "Valid analysis API key or active subscription session required.",
        )

    if keys:
        if not token_as_key or token_as_key not in keys:
            if bearer:
                from app.session_auth import verify_bearer_locally

                local = verify_bearer_locally(bearer)
                if local:
                    return {**local, "entitled": True}
                try:
                    remote = _probe_control_plane_session(bearer)
                except ControlPlaneUnavailable:
                    raise HTTPException(
                        503,
                        "Billing service temporarily unavailable. Try again shortly.",
                    ) from None
                if remote and remote.get("active"):
                    return {
                        "auth": "session",
                        "email": remote.get("email"),
                        "plan": remote.get("plan"),
                        "authenticated": True,
                        "entitled": True,
                    }
            raise HTTPException(401, "Valid analysis API key required.")
        return {"auth": "api_key", "authenticated": True, "entitled": True}

    try:
        return {**require_scan_allowed(), "authenticated": True, "entitled": True}
    except PermissionError as exc:
        raise HTTPException(402, str(exc)) from exc


def require_entitled(auth: dict) -> dict:
    """Write-path gate: inactive trial/subscription → 402 (reads stay open)."""
    if auth.get("auth") == "api_key" or auth.get("entitled"):
        return auth
    if auth.get("entitlement_unavailable"):
        raise HTTPException(
            503,
            "Billing service temporarily unavailable. Try again shortly.",
        )
    raise HTTPException(
        402,
        "Active trial or subscription required to analyze files.",
    )


def resolve_firm_context(auth: dict) -> dict:
    """Map session user_id/email (or API-key ops) → firm_id / user_id."""
    from app.fact_memory.store import get_store

    store = get_store()
    email = (auth.get("email") or "").strip().lower()
    provider_user_id = (auth.get("user_id") or "").strip() or None
    if email:
        return store.ensure_firm_for_email(email, auth_user_id=provider_user_id)
    # API-key / local open mode: shared ops firm (still firm-scoped rows).
    return store.ensure_firm_for_email("ops@casesifter.local", name="Ops / API key")


def require_job_owner(job_id: str, auth: dict) -> dict:
    """Validate job_id shape and firm ownership. Same 404 body for unknown/foreign."""
    from app.fact_memory.store import get_store
    from app.storage import JOB_ID_RE

    if not JOB_ID_RE.fullmatch((job_id or "").strip()):
        raise HTTPException(404, "Unknown or expired job.")
    row = get_store().get_analysis_job(job_id)
    if not row:
        raise HTTPException(404, "Unknown or expired job.")
    if auth.get("auth") == "api_key":
        logger.info("auth=api_key job=%s", job_id[:8])
        return {"job": row, "firm_ctx": resolve_firm_context(auth)}
    firm_ctx = resolve_firm_context(auth)
    if row.get("firm_id") != firm_ctx.get("firm_id"):
        raise HTTPException(404, "Unknown or expired job.")
    return {"job": row, "firm_ctx": firm_ctx}


def _should_persist(auth: dict, firm_ctx: dict | None) -> bool:
    if not settings.persist_facts_enabled():
        return False
    if firm_ctx is None:
        return False
    return bool(firm_ctx.get("persist_facts", True))


def _upsert_durable_job(
    *,
    job_id: str,
    firm_ctx: dict | None,
    matter_id: str | None,
    sha256: str = "",
    status: str = "queued",
    message: str | None = None,
    error: str | None = None,
    document_id: str | None = None,
    attempts: int | None = None,
) -> None:
    if not firm_ctx or not firm_ctx.get("firm_id"):
        return
    try:
        from app.fact_memory.store import get_store

        get_store().upsert_analysis_job(
            job_id=job_id,
            firm_id=firm_ctx["firm_id"],
            matter_id=matter_id,
            document_id=document_id,
            sha256=sha256,
            status=status,
            message=message,
            error=error,
            attempts=attempts,
        )
    except Exception:
        logger.exception("Durable job upsert failed for %s", job_id)


def _update_durable_job(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
    error: str | None = None,
    document_id: str | None = None,
    sha256: str | None = None,
) -> None:
    try:
        from app.fact_memory.store import get_store

        get_store().update_analysis_job(
            job_id,
            status=status,
            message=message,
            error=error,
            document_id=document_id,
            sha256=sha256,
        )
    except Exception:
        logger.exception("Durable job update failed for %s", job_id)


def _durable_interrupted_payload(row: dict) -> dict:
    """Shape a progress-like response when progress.json is gone after a restart."""
    return {
        "job_id": row["job_id"],
        "status": "interrupted",
        "stage": "interrupted",
        "percent": 0,
        "message": row.get("message")
        or "Instance restarted; job was interrupted. Re-submit to continue.",
        "filename": "",
        "error": row.get("error") or "interrupted_by_restart",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "started_at": None,
        "report": None,
        "ephemeral": True,
        "interrupted": True,
        "sha256": row.get("sha256") or "",
        "matter_id": row.get("matter_id"),
        "document_id": row.get("document_id"),
        "attempts": row.get("attempts"),
    }


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
    require_entitled(_auth)
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
    firm_ctx = resolve_firm_context(_auth)
    persist = False
    if resolved_matter:
        persist = _should_persist(_auth, firm_ctx)

        def _load_matter_priors():
            from app.fact_memory.store import get_store

            store = get_store()
            matter = store.get_matter(firm_ctx["firm_id"], resolved_matter)
            if not matter:
                return None, []
            if not persist:
                return matter, []
            return matter, store.list_prior_snapshots(firm_ctx["firm_id"], resolved_matter)

        matter, priors = await asyncio.to_thread(_load_matter_priors)
        if not matter:
            raise HTTPException(404, "Matter not found for this account.")
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

    from app.ingestion import sha256_file

    # Hash off the event loop — large PDFs must not stall /health.
    digest = await asyncio.to_thread(sha256_file, dest)

    # Server dedupe + attempt budget per matter+sha256 (client count resets on reload).
    next_attempts = 1
    if resolved_matter and firm_ctx and digest:
        try:
            from app.fact_memory.store import get_store

            store = get_store()
            active = store.find_active_analysis_job(
                firm_id=firm_ctx["firm_id"],
                matter_id=resolved_matter,
                sha256=digest,
            )
            if active:
                delete_job_files(job_id)
                logger.info(
                    "dedupe active job matter=%s sha=%s existing=%s status=%s",
                    resolved_matter,
                    digest[:12],
                    (active.get("job_id") or "")[:8],
                    active.get("status"),
                )
                return JSONResponse(
                    status_code=200,
                    content={
                        "job_id": active["job_id"],
                        "filename": safe_name,
                        "status": active.get("status") or "queued",
                        "skipped": "already_active",
                        "attempts": active.get("attempts"),
                        "message": "A job for this file is already queued or running.",
                    },
                )
            prior_attempts = store.max_attempts_for_sha(
                firm_id=firm_ctx["firm_id"],
                matter_id=resolved_matter,
                sha256=digest,
            )
            latest = store.latest_job_for_sha(
                firm_id=firm_ctx["firm_id"],
                matter_id=resolved_matter,
                sha256=digest,
            )
            prior_error = str((latest or {}).get("error") or "")
            restart_hits = store.max_restart_interruptions_for_sha(
                firm_id=firm_ctx["firm_id"],
                matter_id=resolved_matter,
                sha256=digest,
            )
            if restart_hits >= 2 or prior_error == "restart_quarantine":
                delete_job_files(job_id)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Needs review — may be crashing the server "
                        f"(running at {max(restart_hits, 2)} instance restarts)."
                    ),
                )
            restart_resume = prior_error in {
                "interrupted_by_restart",
                "local_done_missing_artifacts",
            }
            if prior_attempts >= 3 and not restart_resume:
                delete_job_files(job_id)
                detail = (
                    "Needs re-upload / failed 3 times. "
                    + str((latest or {}).get("error") or (latest or {}).get("message") or "")
                ).strip()
                raise HTTPException(status_code=409, detail=detail)
            # Restart interruptions do not consume an attempt on resume.
            if restart_resume and prior_attempts > 0:
                next_attempts = prior_attempts
            else:
                next_attempts = prior_attempts + 1 if prior_attempts > 0 else 1
        except HTTPException:
            raise
        except Exception:
            logger.exception("active-job dedupe / attempts check failed")

    queue_msg = "Queued for analysis." + (
        " Matter fact memory enabled." if persist else ""
    )
    create_job(job_id, safe_name)
    update_job(job_id, message=queue_msg)
    _upsert_durable_job(
        job_id=job_id,
        firm_ctx=firm_ctx,
        matter_id=resolved_matter,
        sha256=digest,
        status="queued",
        message=queue_msg,
        attempts=next_attempts,
    )
    if persist:
        # Marker so sweeper retains report (facts live in DB; PDF still deleted).
        (dest_dir / "persist.flag").write_text(resolved_matter or "", encoding="utf-8")

    # Schedule off the request thread — backpressure may wait for Gemini slots.
    ensure_scan_executor()
    payload = {
        "job_id": job_id,
        "dest": str(dest),
        "submitted_by": submitted_by,
        "display_name": display_name,
        "doc_kind": doc_kind,
        "prior_snapshots": peers,
        "matter_id": resolved_matter,
        "firm_ctx": firm_ctx,
        "filed_date": filed_date,
        "persist": persist,
    }

    def _kick() -> None:
        try:
            submit_ephemeral_job(payload)
        except Exception:
            logger.exception("Failed to schedule analysis job %s", job_id)

    import threading

    threading.Thread(target=_kick, daemon=True, name=f"schedule-{job_id[:8]}").start()
    return UploadResponse(job_id=job_id, filename=safe_name, status="queued")


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
) -> str | None:
    from app.fact_memory.store import get_store
    from app.ingestion import sha256_file

    store = get_store()
    digest = ""
    if report.audit and report.audit.sha256:
        digest = report.audit.sha256
    elif source.exists():
        digest = sha256_file(source)
    byte_length = None
    if source.exists():
        try:
            byte_length = int(source.stat().st_size)
        except OSError:
            byte_length = None
    if byte_length is None and report.audit and report.audit.file_size_bytes is not None:
        byte_length = int(report.audit.file_size_bytes)
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
            source_size=byte_length,
        )
        logger.info(
            "Persisted facts for matter=%s job=%s persisted=%s deduped=%s",
            matter_id,
            job_id,
            result.get("persisted"),
            result.get("deduped"),
        )
        return result.get("document_id")
    except Exception:
        logger.exception("Failed to persist fact memory for job %s", job_id)
        return None


@v1.get("/jobs/{job_id}")
def get_ephemeral_job(
    job_id: str,
    include_report: bool = Query(default=False),
    _auth: dict = Depends(_require_analysis_auth),
):
    owned = require_job_owner(job_id, _auth)
    firm_id = (owned.get("firm_ctx") or {}).get("firm_id")
    progress = load_progress(job_id)
    if progress is None:
        row = owned.get("job") or {}
        if row.get("status") == "interrupted":
            return _durable_interrupted_payload(row)
        raise HTTPException(404, "Unknown or expired job.")
    # Status polls must stay small. Embedding the report froze the worker on
    # large exhibits and the browser reported that stall as an outage.
    payload = progress.model_dump(mode="json")
    payload["report"] = None
    payload["ephemeral"] = True
    from app.storage import job_dir_existing

    existing = job_dir_existing(job_id)
    persist_flag = (existing / "persist.flag") if existing else None
    if persist_flag is not None and persist_flag.exists():
        payload["ephemeral"] = False
        payload["persist_facts"] = True
        try:
            payload["matter_id"] = persist_flag.read_text(encoding="utf-8").strip() or None
        except OSError:
            payload["matter_id"] = None
    # Timing + queue ETA for the hosted bulk UI.
    try:
        if existing is not None:
            timing_path = existing / "timing.json"
            if timing_path.is_file():
                payload["timing"] = json.loads(timing_path.read_text(encoding="utf-8"))
            else:
                raw = json.loads((existing / "progress.json").read_text(encoding="utf-8"))
                if isinstance(raw.get("timing"), dict):
                    payload["timing"] = raw["timing"]
    except Exception:
        pass
    if progress.status in {"queued", "running", "local_done"}:
        try:
            from app.fact_memory.store import get_store
            from app.gemini_executor import estimate_eta_seconds

            active = get_store().list_analysis_jobs_by_status(
                ["queued", "running", "local_done"],
                firm_id=firm_id,
            )
            position = 1
            for idx, row in enumerate(active, start=1):
                if row.get("job_id") == job_id:
                    position = idx
                    break
            payload["queue_position"] = position
            payload["queued_ahead"] = max(0, position - 1)
            payload["eta_seconds"] = estimate_eta_seconds(queue_position=position)
        except Exception:
            logger.debug("queue eta failed job=%s", job_id[:8], exc_info=True)
    if include_report and progress.status == "complete":
        report = load_report(job_id)
        payload["report"] = report.model_dump(mode="json") if report else None
    return payload


@v1.get("/jobs/{job_id}/report")
def get_ephemeral_report(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    owned = require_job_owner(job_id, _auth)
    report = load_report(job_id)
    if report is None:
        # Persisted jobs delete disk reports at terminal; serve from Postgres.
        row = owned.get("job") or {}
        doc_id = row.get("document_id")
        firm_id = (owned.get("firm_ctx") or {}).get("firm_id")
        matter_id = row.get("matter_id")
        if doc_id and firm_id and matter_id:
            try:
                from app.fact_memory.store import get_store

                durable = get_store().get_document_report(firm_id, matter_id, doc_id)
                if isinstance(durable, dict):
                    return JSONResponse(durable)
            except Exception:
                logger.debug("durable report fallback failed job=%s", job_id[:8], exc_info=True)
        progress = load_progress(job_id)
        if progress is None and not row:
            raise HTTPException(404, "Unknown or expired job.")
        if progress is None:
            raise HTTPException(404, "Unknown or expired job.")
        raise HTTPException(409, "Report is not ready yet.")
    return JSONResponse(report.model_dump(mode="json"))


@v1.delete("/jobs/{job_id}")
def purge_ephemeral_job(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    require_job_owner(job_id, _auth)
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
    if _auth.get("auth") != "api_key":
        raise HTTPException(403, "API key required.")
    return {"removed": sweep_stale_jobs()}
