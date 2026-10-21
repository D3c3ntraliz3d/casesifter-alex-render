from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import logging

from app.edge_security import csrf_block_response, docs_kwargs, install_api_headers, origin_blocked

from app import cases_db
from app.api import public_router, router
from app.config import settings
from app.ephemeral import ensure_sweeper
from app.fact_memory.matters_api import matters_router
from app.fact_memory.store import get_store
from app.scan_watchdog import ensure_watchdog
from app.sentry_scrub import init_sentry
from app.internal_jobs import internal_router
from app.v1_analysis import v1
import os

logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="CaseSifter — local store, ephemeral remote analysis.",
    **docs_kwargs(settings.app_env),
)

_mode = settings.resolve_app_mode()
_origins = settings.cors_origin_list()
if _mode == "analysis" and ("*" in _origins or not _origins):
    # Analysis service must not reflect arbitrary origins.
    # Include alex + john hosted apps so an empty CORS env does not strand either stack.
    _origins = [
        "https://app.casesifter.com",
        "https://www.casesifter.com",
        "https://casesifter.com",
        "https://casesifter-app.onrender.com",
        "https://casesifter-marketing.onrender.com",
        "https://casesifter-alex-app.onrender.com",
        "https://casesifter-alex-marketing.onrender.com",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_api_headers(app)

_ORIGIN_ALLOW = {origin.rstrip("/") for origin in _origins}
_PRODUCTION = (settings.app_env or "").strip().lower() == "production"


@app.middleware("http")
async def _cookie_csrf(request: Request, call_next):
    """Cookie sessions are not sent as a Bearer header, so check Origin."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get("cs_session"):
        authorization = request.headers.get("authorization") or ""
        if not authorization.lower().startswith("bearer "):
            if origin_blocked(
                request.headers.get("origin") or "",
                _ORIGIN_ALLOW,
                production=_PRODUCTION,
                referer=request.headers.get("referer") or "",
                sec_fetch_site=request.headers.get("sec-fetch-site") or "",
            ):
                return csrf_block_response()
    return await call_next(request)

# analysis = hosted hybrid (no vault). vault = local John testing (full case API).
if _mode == "analysis":
    app.include_router(public_router, prefix="/api")
    app.include_router(v1, prefix="/api")
    app.include_router(matters_router, prefix="/api")
    app.include_router(internal_router, prefix="/api")
else:
    app.include_router(router, prefix="/api")
    app.include_router(v1, prefix="/api")
    app.include_router(matters_router, prefix="/api")
    app.include_router(internal_router, prefix="/api")


init_sentry(service="analysis-api")


@app.on_event("startup")
def _startup() -> None:
    # Sync settings → env for fact_memory encryption / DATABASE_URL readers.
    if settings.database_url and not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = settings.database_url
    if settings.fact_memory_master_key and not os.environ.get("FACT_MEMORY_MASTER_KEY"):
        os.environ["FACT_MEMORY_MASTER_KEY"] = settings.fact_memory_master_key
    # Analysis hosted mode: SESSION_SECRET is mandatory (no default / no probe-only auth).
    if _mode == "analysis":
        from app.session_auth import require_session_secret_configured, session_secret

        if (settings.app_env or "").strip().lower() == "production":
            require_session_secret_configured()
        elif not session_secret():
            logger.error(
                "SESSION_SECRET is empty — browser sessions will be rejected "
                "(fail closed; no default secret)."
            )
    # Soft-launch: /api/v1 accepts ANALYSIS_API_KEYS or a locally verified session.
    if _mode == "analysis" and not settings.analysis_api_key_set() and not settings.cloud_control_url:
        logger.warning(
            "APP_MODE=analysis but ANALYSIS_API_KEYS and CLOUD_CONTROL_URL are empty — "
            "/api/v1 will return 401."
        )
    if (
        _mode == "analysis"
        and (settings.app_env or "").strip().lower() == "production"
        and not (os.environ.get("DATABASE_URL") or settings.database_url or "").strip()
    ):
        raise RuntimeError(
            "DATABASE_URL is required for production analysis. "
            "Without it, matters are stored on ephemeral SQLite and disappear after restart."
        )
    try:
        from app.image_deps import missing_image_forensics_deps

        missing_img = missing_image_forensics_deps()
        if missing_img:
            logger.error(
                "Image forensics packages missing (%s) — Image check will stay red until "
                "pip install -r backend/requirements.txt and a re-scan.",
                ", ".join(missing_img),
            )
        else:
            logger.info("Image forensics packages present (cv2, pypdfium2, numpy)")
    except Exception as exc:
        logger.warning("Image forensics dep probe failed: %s", exc)
    cases_db.ensure_schema()
    try:
        from app.fact_memory.models import fact_memory_backend_label

        store = get_store()  # init SQLite/Postgres fact memory schema
        logger.info("Fact memory backend=%s", fact_memory_backend_label())
        n = store.mark_active_jobs_interrupted()
        if n:
            logger.warning("Marked %s queued/running analysis job(s) interrupted after restart", n)
    except Exception as exc:
        logger.warning("Fact memory store init failed: %s", exc)
    ensure_sweeper()
    ensure_watchdog()
    try:
        from app.fact_extract_backfill import ensure_extract_backfill_worker

        ensure_extract_backfill_worker()
    except Exception as exc:
        logger.warning("Fact-extract backfill worker init failed: %s", exc)
    try:
        from app.scan_executor import ensure_scan_executor
        from app.gemini_executor import ensure_gemini_executor

        ensure_scan_executor()
        ensure_gemini_executor()
    except Exception as exc:
        logger.warning("Scan process pool init failed: %s", exc)
    try:
        from app.job_recovery import recover_local_done_jobs

        recovered = recover_local_done_jobs()
        if recovered.get("seen"):
            logger.warning("local_done recovery: %s", recovered)
    except Exception as exc:
        logger.warning("local_done recovery failed: %s", exc)
    from app.config import gemini_model_is_valid
    from app.gemini_tasks import startup_validate_and_probe, task_config, TASK_D_VISUAL

    try:
        visual_primary = task_config(TASK_D_VISUAL).primary
    except Exception:
        visual_primary = ""
    model_ok = gemini_model_is_valid(visual_primary)
    startup_validate_and_probe()
    if not model_ok:
        # Never echo a malformed/secret-shaped model string into logs.
        logger.error(
            "TASK_D_VISUAL_PRIMARY failed validation (expected ^gemini-[\\w.-]+$). "
            "Multimodal Gemini path disabled. APP_MODE=%s",
            _mode,
        )
    if settings.local_only_review:
        logger.warning(
            "LOCAL_ONLY_REVIEW is on — forensic scans are labeled local-only and "
            "are not treated as a completed visual review."
        )
    if not settings.gemini_api_key:
        logger.warning(
            "GEMINI_API_KEY is not set — exhibit multimodal / historical fraud review is OFF. "
            "Forensic exhibits that require a visual review will not be cleared as low risk. "
            "Set GEMINI_API_KEY and CONTEXTUAL_MULTIMODAL=true, or set LOCAL_ONLY_REVIEW=true "
            "to label an explicit local-only scan."
        )
    elif not settings.contextual_multimodal:
        logger.warning(
            "CONTEXTUAL_MULTIMODAL is false — Gemini will not receive native exhibit PDFs. "
            "Set CONTEXTUAL_MULTIMODAL=true to restore the scanned-exhibit pause and fraud path."
        )
    elif not model_ok:
        logger.error(
            "Refusing multimodal Gemini calls: TASK_D_VISUAL_PRIMARY does not match "
            "^gemini-[\\w.-]+$. Fix the env var before enabling CONTEXTUAL_MULTIMODAL."
        )
    else:
        logger.info(
            "Exhibit multimodal enabled (gemini_model_valid=%s). APP_MODE=%s",
            True,
            _mode,
        )
    logger.info(
        "Fact memory: PERSIST_FACTS=%s SOURCE_FILE_RETENTION=%s "
        "SCAN_MAX_CONCURRENT=%s GEMINI_MAX_CONCURRENT=%s GEMINI_MAX_INFLIGHT_MB=%s",
        settings.persist_facts,
        settings.source_file_retention,
        max(1, int(settings.scan_max_concurrent or 1)),
        max(1, int(settings.gemini_max_concurrent or 2)),
        max(16, int(settings.gemini_max_inflight_mb or 150)),
    )
    try:
        from app.llm_verify import resolve_verify_provider

        _verifier = resolve_verify_provider()
    except Exception:
        _verifier = "unknown"
    logger.info(
        "Gotcha verifier: %s (GOTCHA_VERIFY_PROVIDER=%s); case-impact judge: %s",
        _verifier,
        settings.gotcha_verify_provider,
        "claude" if (settings.anthropic_api_key or "").strip() else "local rules",
    )


@app.on_event("shutdown")
def _shutdown() -> None:
    try:
        from app.scan_executor import shutdown_scan_executor

        shutdown_scan_executor()
    except Exception:
        pass


@app.get("/")
def root():
    payload = {
        "name": settings.app_name,
        "version": settings.app_version,
        "app_mode": _mode,
        "health": "/api/health",
    }
    if not _PRODUCTION:
        payload["docs"] = "/docs"
    return payload
