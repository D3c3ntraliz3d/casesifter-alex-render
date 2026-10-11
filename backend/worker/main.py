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
    from app.job_queue import run_local_worker_loop
    from app.worker_auth import require_worker_token_configured
    from worker.run_job import WorkerJobError, process_queue_message

    if (settings.scan_backend or "inline").strip().lower() not in {"worker", "local_worker", "cloudrun"}:
        logger.warning(
            "SCAN_BACKEND=%s — worker will still poll the queue when started.",
            settings.scan_backend,
        )
    require_worker_token_configured()
    logger.info(
        "scan worker starting callback=%s blob=%s queue=%s max_attempts=%s",
        settings.worker_callback_base_url,
        settings.blob_backend,
        settings.queue_backend,
        settings.worker_max_attempts,
    )

    def _handler(msg):
        try:
            process_queue_message(msg)
        except WorkerJobError:
            # run_local_worker_loop handles retry / dead-letter.
            raise

    stop = threading.Event()

    def _on_term(signum, _frame):
        # Render sends SIGTERM on deploy/scale-down: stop claiming new jobs. A job
        # cut off mid-run is re-claimed by another worker once its heartbeat lapses.
        logger.info("signal %s — draining, no new claims", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_term)
    try:
        run_local_worker_loop(_handler, idle_sleep_s=0.5, stop_event=stop)
    except KeyboardInterrupt:
        stop.set()
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
