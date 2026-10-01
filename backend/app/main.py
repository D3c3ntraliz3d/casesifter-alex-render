from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app import cases_db
from app.api import public_router, router
from app.config import settings
from app.ephemeral import ensure_sweeper
from app.scan_watchdog import ensure_watchdog
from app.sentry_scrub import init_sentry
from app.v1_analysis import v1

logger = logging.getLogger("uvicorn.error")

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="CaseSifter — local store, ephemeral remote analysis.",
)

_mode = (settings.app_mode or "vault").strip().lower()
_origins = settings.cors_origin_list()
if _mode == "analysis" and ("*" in _origins or not _origins):
    # Analysis service must not reflect arbitrary origins.
    _origins = [
        "https://casesifter.com",
        "https://www.casesifter.com",
        "https://casesifter-marketing.onrender.com",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# analysis = hosted hybrid (no vault). vault = local John testing (full case API).
if _mode == "analysis":
    app.include_router(public_router, prefix="/api")
    app.include_router(v1, prefix="/api")
else:
    app.include_router(router, prefix="/api")
    app.include_router(v1, prefix="/api")


init_sentry(service="analysis-api")


@app.on_event("startup")
def _startup() -> None:
    if _mode == "analysis" and not settings.analysis_api_key_set():
        if (settings.app_env or "").strip().lower() == "production":
            raise RuntimeError(
                "APP_MODE=analysis requires ANALYSIS_API_KEYS when APP_ENV=production."
            )
        logger.warning(
            "APP_MODE=analysis but ANALYSIS_API_KEYS is empty — /api/v1 will return 401."
        )
    cases_db.ensure_schema()
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


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "app_mode": _mode,
        "docs": "/docs",
        "health": "/api/health",
    }
