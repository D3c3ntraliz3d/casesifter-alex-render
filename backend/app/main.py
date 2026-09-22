from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import cases_db
from app.api import router
from app.config import settings
from app.ephemeral import ensure_sweeper
from app.scan_watchdog import ensure_watchdog
from app.sentry_scrub import init_sentry
from app.v1_analysis import v1

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


@app.get("/")
def root():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/api/health",
    }
