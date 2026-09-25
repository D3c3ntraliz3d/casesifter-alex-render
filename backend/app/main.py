from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from app import cases_db
from app.api import router
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")
app.include_router(v1, prefix="/api")


init_sentry(service="analysis-api")


@app.on_event("startup")
def _startup() -> None:
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
            "Exhibit multimodal enabled (Gemini model=%s).",
            settings.gemini_model,
        )


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/api/health",
    }
