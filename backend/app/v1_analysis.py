"""Public v1 analysis API for the thin desktop client (ephemeral jobs).

When matter_id is supplied and the firm has persist_facts, priors are loaded from
fact memory, compare_to_case runs, and facts/findings are persisted. With
SOURCE_FILE_RETENTION=immediate the PDF is dropped after scan; with s3/keep the
tiny source is retained in object storage for re-scan/preview.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.case_chat import ask_case_from_packet
from app.config import settings
from app.entitlement import require_scan_allowed
from app.ephemeral import (
    delete_job_files,
    ensure_sweeper,
    sweep_stale_jobs,
)
from app.models import Report, UploadResponse
from app.product_events import emit_chat_asked
from app.scan_executor import ensure_scan_executor
from app.scan_watchdog import ensure_watchdog
from app.storage import create_job, job_dir, load_progress, load_report, update_job

v1 = APIRouter(prefix="/v1", tags=["v1-analysis"])
logger = logging.getLogger(__name__)

# Short-lived cache: session bearer → entitlement probe (soft-launch browser auth).
_SESSION_ENTITLEMENT_CACHE: dict[str, tuple[float, dict]] = {}
_SESSION_CACHE_TTL_S = 45.0


def _durable_source_key(
    *,
    firm_id: str | None,
    matter_id: str | None,
    sha256: str | None,
    preferred_key: str | None = None,
) -> str | None:
    """Return a durable S3/R2 key only when the object is actually present."""
    from app.scan_dispatch import resolve_retained_source_key

    return resolve_retained_source_key(
        firm_id=firm_id,
        matter_id=matter_id,
        sha256=sha256,
        preferred_key=preferred_key,
    )


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
        # Keep Exhibit *display* name; refine kind when body is a statement/bill.
        from app.scan_profile import looks_like_statement_text

        final_kind = "bill" if looks_like_statement_text(text_sample or "") else "exhibit"
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
    cs_session: str | None = Cookie(default=None),
) -> dict:
    """Authenticate only — does not require an active paid/trial entitlement."""
    keys = settings.analysis_api_key_set()
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    elif cs_session:
        bearer = cs_session.strip()
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
                "org_owner_email": (remote or {}).get("org_owner_email"),
                "account_role": (remote or {}).get("account_role"),
                "org_id": (remote or {}).get("org_id"),
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
    join_owner = (auth.get("org_owner_email") or "").strip().lower() or None
    account_role = (auth.get("account_role") or "").strip().lower() or None
    if email:
        return store.ensure_firm_for_email(
            email,
            auth_user_id=provider_user_id,
            join_owner_email=join_owner if join_owner and join_owner != email else None,
            account_role=account_role,
        )
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
    firm_ctx = resolve_firm_context(auth)
    # Ops API keys map to the shared ops firm — never cross-read another firm's jobs.
    if row.get("firm_id") and row.get("firm_id") != firm_ctx.get("firm_id"):
        raise HTTPException(404, "Unknown or expired job.")
    if auth.get("auth") == "api_key":
        logger.info("auth=api_key job=%s firm=%s", job_id[:8], (firm_ctx.get("firm_id") or "")[:8])
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
    original_filename: str | None = None,
    display_name: str | None = None,
    doc_kind: str | None = None,
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
            user_id=firm_ctx.get("user_id"),
            sha256=sha256,
            status=status,
            message=message,
            error=error,
            attempts=attempts,
            original_filename=original_filename,
            display_name=display_name,
            doc_kind=doc_kind,
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
    ai_review: str | None = None,
    ai_review_reason: str | None = None,
    allow_rerun: bool = False,
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
            ai_review=ai_review,
            ai_review_reason=ai_review_reason,
            allow_rerun=allow_rerun,
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


class SessionAcceptBody(BaseModel):
    code: str = Field(min_length=8, max_length=200)


def _exchange_analysis_code(code: str) -> str:
    """Trade a one-time control-plane code for a session token. Server-side only."""
    base = (settings.cloud_control_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(503, "Control plane is not configured.")
    url = f"{base}/v1/session/exchange"
    payload = json.dumps({"code": code}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    secret = (os.environ.get("SESSION_SECRET") or "").strip()
    if secret:
        headers["X-Internal-Session"] = secret
    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HTTPException(401, "Invalid or expired exchange code.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(503, "Control plane is temporarily unavailable.") from exc
    token = str((data or {}).get("token") or "").strip()
    if not token:
        raise HTTPException(401, "Invalid or expired exchange code.")
    return token


@v1.post("/session/accept")
def accept_analysis_session(body: SessionAcceptBody):
    """Set the analysis-host session cookie. The token is not returned to the browser."""
    from app.session_cookie import set_session_cookie

    token = _exchange_analysis_code(body.code)
    response = JSONResponse({"ok": True})
    ttl_days = int(float(os.environ.get("SESSION_TTL_DAYS", "30") or 30))
    set_session_cookie(response, token, max_age=ttl_days * 86400)
    return response


@v1.post("/session/logout")
def logout_analysis_session():
    from app.session_cookie import clear_session_cookie

    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@v1.post("/jobs", response_model=UploadResponse)
async def create_ephemeral_job(
    request: Request,
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
    matter: dict | None = None
    if resolved_matter:
        persist = _should_persist(_auth, firm_ctx)

        def _load_matter_priors():
            from app.fact_memory.store import get_store

            store = get_store()
            ctx = firm_ctx
            row = store.get_matter(ctx["firm_id"], resolved_matter)
            if not row:
                # Email-first resolve already ran; try reclaim when the SPA still
                # has a matter id from a firm this account previously owned.
                email = (ctx.get("email") or _auth.get("email") or "").strip().lower()
                recovered = None
                if email:
                    recovered = store.reclaim_access_to_matter(
                        resolved_matter,
                        email=email,
                        auth_user_id=_auth.get("user_id") or ctx.get("user_id"),
                    )
                if recovered:
                    ctx = recovered
                    row = store.get_matter(ctx["firm_id"], resolved_matter)
                    logger.info(
                        "matter reclaim ok matter=%s firm=%s email=%s",
                        resolved_matter[:8],
                        (ctx.get("firm_id") or "")[:8],
                        email,
                    )
                else:
                    logger.info(
                        "matter miss firm=%s matter=%s email=%s auth=%s",
                        (ctx.get("firm_id") or "")[:8],
                        resolved_matter[:8],
                        email or "-",
                        (_auth.get("auth") or "-"),
                    )
            if not row:
                return None, [], ctx
            if not _should_persist(_auth, ctx):
                return row, [], ctx
            return row, store.list_prior_snapshots(ctx["firm_id"], resolved_matter), ctx

        matter, priors, firm_ctx = await asyncio.to_thread(_load_matter_priors)
        persist = _should_persist(_auth, firm_ctx)
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

    index_warn: dict | None = None
    if matter:
        try:
            from app.auto_title import index_mismatch_warning

            index_warn = index_mismatch_warning(safe_name, matter.get("index_number"))
        except Exception:
            logger.exception("index hygiene check failed matter=%s", resolved_matter)

    queue_msg = "Queued for analysis." + (
        " Matter fact memory enabled." if persist else ""
    )
    if index_warn and index_warn.get("message"):
        queue_msg = f"{queue_msg} {index_warn['message']}"
    label_name, label_kind = _resolve_upload_labels(safe_name, display_name, doc_kind)
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
        original_filename=safe_name,
        display_name=label_name or safe_name,
        doc_kind=label_kind,
    )
    if persist:
        # Marker so sweeper retains report (facts live in DB; PDF still deleted).
        (dest_dir / "persist.flag").write_text(resolved_matter or "", encoding="utf-8")

    # Retain tiny source PDFs in object storage BEFORE the async kick so a
    # worker/ephemeral cleanup cannot race the durable put.
    durable_key = None
    if persist and firm_ctx and resolved_matter and digest and settings.keep_source_in_blob:
        try:
            from app.scan_dispatch import retain_source_blob

            durable_key = retain_source_blob(
                firm_id=firm_ctx.get("firm_id"),
                matter_id=resolved_matter,
                sha256=digest,
                source_path=dest,
            )
        except Exception:
            logger.exception("upload-time durable retain failed job=%s", job_id[:8])

    # Schedule off the request thread — inline backpressure or worker enqueue.
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
        "sha256": digest,
        "source_blob_key": durable_key,
    }

    def _kick() -> None:
        try:
            from app.scan_dispatch import dispatch_ephemeral_job

            dispatch_ephemeral_job(payload)
        except Exception:
            logger.exception("Failed to schedule analysis job %s", job_id)

    import threading

    threading.Thread(target=_kick, daemon=True, name=f"schedule-{job_id[:8]}").start()
    if firm_ctx and firm_ctx.get("firm_id"):
        try:
            from app.fact_memory.store import get_store

            request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex
            get_store().audit(
                firm_ctx.get("firm_id"),
                firm_ctx.get("user_id"),
                "scan.start",
                job_id,
                request_id=request_id,
            )
        except Exception:
            logger.exception("scan.start audit failed job=%s", job_id[:8])
    return UploadResponse(
        job_id=job_id,
        filename=safe_name,
        status="queued",
        index_warning=(index_warn or {}).get("message"),
        filename_index=(index_warn or {}).get("filename_index"),
        matter_index=(index_warn or {}).get("matter_index"),
    )


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
    source_blob_key: str | None = None,
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
    # Last-chance retain when the source file is still on this host (inline path).
    preferred_key = (source_blob_key or "").strip() or None
    if (
        settings.keep_source_in_blob
        and source.exists()
        and firm_ctx.get("firm_id")
        and matter_id
        and digest
    ):
        try:
            from app.scan_dispatch import retain_source_blob

            preferred_key = (
                retain_source_blob(
                    firm_id=firm_ctx.get("firm_id"),
                    matter_id=matter_id,
                    sha256=digest,
                    source_path=source,
                )
                or preferred_key
            )
        except Exception:
            logger.exception("persist-time durable retain failed job=%s", job_id[:8])
    resolved_blob_key = _durable_source_key(
        firm_id=firm_ctx.get("firm_id"),
        matter_id=matter_id,
        sha256=digest,
        preferred_key=preferred_key,
    )
    if settings.keep_source_in_blob and digest and not resolved_blob_key:
        logger.warning(
            "persist without durable source job=%s matter=%s",
            job_id[:8],
            str(matter_id)[:8],
        )
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
    # Inline heatmaps + page screenshots before durable encrypt — source/artifacts
    # are deleted after persist, so the export PDF must already carry the crops.
    try:
        from app.report.generator import prepare_report_for_export

        src_for_shots = source if source.exists() else None
        report_dict = prepare_report_for_export(report, source=src_for_shots).model_dump(
            mode="json"
        )
    except Exception:
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
            source_blob_key=resolved_blob_key,
            extraction_status=comparison.get("extraction_status"),
            extraction_error=comparison.get("extraction_error"),
            extraction_retry_done=bool(comparison.get("extraction_retry_done")),
        )
        try:
            from app.extraction_status import incomplete_prior_snapshots, priors_pending_message
            from app.fact_extract_backfill import (
                enqueue_extraction_backfill,
                register_compare_refresh_after_backfill,
            )

            anchor_id = result.get("document_id")
            priors = store.list_prior_snapshots(
                firm_ctx["firm_id"],
                matter_id,
                exclude_document_id=anchor_id,
            )
            incomplete = incomplete_prior_snapshots(priors)
            pending_n = len(incomplete)
            if pending_n and isinstance(report_dict.get("comparison"), dict):
                cc = dict(report_dict["comparison"].get("case_consistency") or {})
                cc["priors_pending_extraction"] = pending_n
                cc["priors_pending_message"] = priors_pending_message(pending_n)
                report_dict["comparison"]["case_consistency"] = cc
                report_dict["comparison"]["priors_pending_extraction"] = pending_n
                report_dict["comparison"]["priors_pending_message"] = (
                    priors_pending_message(pending_n)
                )
                if anchor_id:
                    store.update_document_report(
                        firm_ctx["firm_id"], matter_id, anchor_id, report_dict
                    )
            queued_prior_ids = enqueue_extraction_backfill(
                priors,
                scope="matter",
                firm_id=firm_ctx["firm_id"],
                matter_id=matter_id,
            )
            if queued_prior_ids and anchor_id:
                register_compare_refresh_after_backfill(
                    scope="matter",
                    anchor_document_id=anchor_id,
                    waiting_prior_ids=queued_prior_ids,
                    job_id=job_id,
                    firm_id=firm_ctx["firm_id"],
                    matter_id=matter_id,
                    new_facts=facts if isinstance(facts, list) else [],
                    new_entities=entities if isinstance(entities, dict) else {},
                    new_display_name=display_name or source.name,
                    new_doc_kind=doc_kind,
                    new_fingerprint=comparison.get("text_fingerprint"),
                    new_sha256=digest,
                    new_text=(comparison.get("text_excerpt") or "")[:24000],
                    new_page_offsets=comparison.get("page_offsets"),
                )
        except Exception:
            logger.exception("Failed to enqueue fact-extract backfill for matter=%s", matter_id)
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
    if progress.status == "complete":
        # Lightweight AI-review badge fields (avoid embedding the full report).
        try:
            row = owned.get("job") or {}
            if row.get("ai_review"):
                payload["ai_review"] = row.get("ai_review")
                payload["ai_review_reason"] = row.get("ai_review_reason")
            else:
                report_lite = load_report(job_id)
                if report_lite is not None:
                    payload["ai_review"] = getattr(report_lite, "ai_review", None)
                    payload["ai_review_reason"] = getattr(report_lite, "ai_review_reason", None)
        except Exception:
            logger.debug("ai_review status fields failed job=%s", job_id[:8], exc_info=True)
    if include_report and progress.status == "complete":
        report = load_report(job_id)
        from app.case_conflict_toggle import strip_report

        payload["report"] = strip_report(report.model_dump(mode="json")) if report else None
    return payload




def _resolve_report_dict(job_id: str, owned: dict) -> dict | None:
    """Load report JSON from disk or durable fact-memory (post-ephemeral delete)."""
    report = load_report(job_id)
    if report is not None:
        return report.model_dump(mode="json")
    row = owned.get("job") or {}
    doc_id = row.get("document_id")
    firm_id = (owned.get("firm_ctx") or {}).get("firm_id")
    matter_id = row.get("matter_id")
    if doc_id and firm_id and matter_id:
        try:
            from app.fact_memory.store import get_store

            durable = get_store().get_document_report(firm_id, matter_id, doc_id)
            if isinstance(durable, dict):
                return durable
        except Exception:
            logger.debug("durable report fallback failed job=%s", job_id[:8], exc_info=True)
    return None


@v1.get("/jobs/{job_id}/report")
def get_ephemeral_report(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    owned = require_job_owner(job_id, _auth)
    raw = _resolve_report_dict(job_id, owned)
    if raw is None:
        progress = load_progress(job_id)
        row = owned.get("job") or {}
        if progress is None and not row:
            raise HTTPException(404, "Unknown or expired job.")
        if progress is None:
            raise HTTPException(404, "Unknown or expired job.")
        raise HTTPException(409, "Report is not ready yet.")
    from app.case_conflict_toggle import strip_report

    return JSONResponse(strip_report(raw))


@v1.get("/jobs/{job_id}/export/{kind}")
def export_ephemeral_report(job_id: str, kind: str, _auth: dict = Depends(_require_analysis_auth)):
    """Regenerate HTML/PDF/JSON from durable report (disk files are deleted after persist)."""
    kind = (kind or "").strip().lower()
    if kind not in {"html", "pdf", "json"}:
        raise HTTPException(400, "Export kind must be html, pdf, or json.")
    owned = require_job_owner(job_id, _auth)
    raw = _resolve_report_dict(job_id, owned)
    if raw is None:
        progress = load_progress(job_id)
        if progress is None:
            raise HTTPException(404, "Unknown or expired job.")
        raise HTTPException(409, "Report is not ready yet.")
    from app.case_conflict_toggle import strip_report

    stripped = strip_report(raw)
    filename = f"exhibit-report-{job_id}.{kind}"
    if kind == "json":
        body = json.dumps(stripped, indent=2, default=str).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    try:
        report = Report.model_validate(stripped)
    except Exception as exc:
        logger.warning("export validate failed job=%s: %s", job_id[:8], exc)
        raise HTTPException(500, "Stored report could not be rendered.") from exc
    from app.report.generator import render_html, render_pdf_bytes

    if kind == "html":
        html = render_html(report)
        return Response(
            content=html.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    pdf_bytes = render_pdf_bytes(report)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@v1.post("/jobs/{job_id}/cancel")
def cancel_ephemeral_job(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    """Cancel a queued or running scan. Idempotent when already terminal."""
    owned = require_job_owner(job_id, _auth)
    firm_id = (owned.get("firm_ctx") or {}).get("firm_id")
    from app.job_cancel import cancel_analysis_job

    try:
        return cancel_analysis_job(job_id, firm_id=firm_id)
    except ValueError as exc:
        raise HTTPException(404, "Unknown or expired job.") from exc
    except PermissionError as exc:
        raise HTTPException(404, "Unknown or expired job.") from exc


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
