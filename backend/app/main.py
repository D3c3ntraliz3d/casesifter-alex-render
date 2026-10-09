from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app import cases_db
from app.api import public_router, router
from app.config import settings
from app.ephemeral import ensure_sweeper
from app.fact_memory.matters_api import matters_router
from app.fact_memory.store import get_store
from app.scan_watchdog import ensure_watchdog
from app.sentry_scrub import init_sentry
from app.v1_analysis import v1
import os

logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="CaseSifter — local store, ephemeral remote analysis.",
)

_mode = settings.resolve_app_mode()
_origins = settings.cors_origin_list()
if _mode == "analysis" and ("*" in _origins or not _origins):
    # Analysis service must not reflect arbitrary origins.
    _origins = [
        "https://app.casesifter.com",
        "https://www.casesifter.com",
        "https://casesifter.com",
        "https://casesifter-app.onrender.com",
        "https://casesifter-marketing.onrender.com",
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

# analysis = hosted hybrid (no vault). vault = local John testing (full case API).
if _mode == "analysis":
    app.include_router(public_router, prefix="/api")
    app.include_router(v1, prefix="/api")
    app.include_router(matters_router, prefix="/api")
else:
    app.include_router(router, prefix="/api")
    app.include_router(v1, prefix="/api")
    app.include_router(matters_router, prefix="/api")


init_sentry(service="analysis-api")


@app.on_event("startup")
def _startup() -> None:
    # Sync settings → env for fact_memory encryption / DATABASE_URL readers.
    if settings.database_url and not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = settings.database_url
    if settings.fact_memory_master_key and not os.environ.get("FACT_MEMORY_MASTER_KEY"):
        os.environ["FACT_MEMORY_MASTER_KEY"] = settings.fact_memory_master_key
    # Do not refuse boot when keys are missing — vault stays unmounted.
    # Soft-launch: /api/v1 accepts ANALYSIS_API_KEYS or an active control-plane session.
    if _mode == "analysis" and not settings.analysis_api_key_set() and not settings.cloud_control_url:
        logger.warning(
            "APP_MODE=analysis but ANALYSIS_API_KEYS and CLOUD_CONTROL_URL are empty — "
            "/api/v1 will return 401."
        )
    cases_db.ensure_schema()
    try:
        store = get_store()  # init SQLite/Postgres fact memory schema
        n = store.mark_active_jobs_interrupted()
        if n:
            logger.warning("Marked %s queued/running analysis job(s) interrupted after restart", n)
    except Exception as exc:
        logger.warning("Fact memory store init failed: %s", exc)
    ensure_sweeper()
    ensure_watchdog()
    if not settings.gemini_api_key:
        logger.warning(
            "GEMINI_API_KEY is not set — exhibit multimodal / historical fraud review is OFF. "
            "Scanned exhibits will not pause on Gemini. Set GEMINI_API_KEY and "
            "CONTEXTUAL_MULTIMODAL=true in .env."
        )
    elif not settings.contextual_multimodal:
        logger.warning(
            "CONTEXTUAL_MULTIMODAL is false — Gemini will not receive native exhibit PDFs. "
            "Set CONTEXTUAL_MULTIMODAL=true to restore the scanned-exhibit pause and fraud path."
        )
    else:
        logger.info(
            "Exhibit multimodal enabled (Gemini model=%s). APP_MODE=%s",
            settings.gemini_model,
            _mode,
        )
    logger.info(
        "Fact memory: PERSIST_FACTS=%s SOURCE_FILE_RETENTION=%s",
        settings.persist_facts,
        settings.source_file_retention,
    )


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "app_mode": _mode,
        "docs": "/docs",
        "health": "/api/health",
    }
