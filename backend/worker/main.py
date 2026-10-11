"""Worker entrypoint: poll the job queue (local folder or shared Redis)."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

# Ensure backend root is on sys.path when launched as ``python -m worker.main``.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
logger = logging.getLogger("worker")


def main() -> None:
    from app.config import settings
    from worker.pool import run_worker_pool
    from app.worker_auth import require_worker_token_configured
    
    if (settings.scan_backend or "inline").strip().lower() not in {"worker", "local_worker", "cloudrun"}:
        logger.warning(
            "SCAN_BACKEND=%s — worker will still poll the queue when started.",
            settings.scan_backend,
        )
    require_worker_token_configured()
    # Temp reports are encrypted with a process-local random key; the worker
    # never receives FACT_MEMORY_MASTER_KEY.
    from app.storage import use_ephemeral_report_key

    use_ephemeral_report_key()
    logger.info(
        "scan worker starting callback=%s blob=%s queue=%s max_attempts=%s concurrency=%s",
        settings.worker_callback_base_url,
        settings.blob_backend,
        settings.queue_backend,
        settings.worker_max_attempts,
        settings.worker_concurrency,
    )

    stop = threading.Event()

    def _on_term(signum, _frame):
        # Render sends SIGTERM on deploy/scale-down: stop claiming new jobs. A job
        # cut off mid-run is re-claimed by another worker once its heartbeat lapses.
        logger.info("signal %s — draining, no new claims", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_term)
    try:
        run_worker_pool(stop_event=stop)
    except KeyboardInterrupt:
        stop.set()
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
