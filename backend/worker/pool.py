"""Run several scan jobs at once, each in its own child process.

Why processes, not threads: the scan pipeline swaps the process-global
``settings.data_dir`` while it runs, so two jobs in one process would trample
each other. A child process also contains crashes and memory blowups.

The parent owns all queue state (claim / heartbeat / complete / release /
fail); children only run the pipeline and report success via exit code.
"""

from __future__ import annotations

import importlib
import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass

from app.config import settings
from app.job_queue import (
    VISIBILITY_TIMEOUT_S,
    JobQueue,
    QueueMessage,
    _start_heartbeat,
    get_job_queue,
)

logger = logging.getLogger(__name__)

DEFAULT_TARGET = "worker.pool:_run_scan_job"


def _run_scan_job(message_json: str) -> None:
    """Child entry for real scans."""
    from app.storage import use_ephemeral_report_key
    from worker.run_job import process_queue_message

    use_ephemeral_report_key()
    process_queue_message(QueueMessage.from_json(message_json))


def _child_main(target: str, message_json: str) -> None:
    import logging as _logging
    import sys

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s [worker-job] %(message)s",
    )
    mod_name, func_name = target.split(":", 1)
    func = getattr(importlib.import_module(mod_name), func_name)
    try:
        func(message_json)
    except BaseException:
        _logging.getLogger("worker").exception("job child failed")
        sys.exit(1)
    sys.exit(0)


@dataclass
class _Slot:
    msg: QueueMessage
    proc: mp.process.BaseProcess
    beat: threading.Event
    started: float


def _retry_or_fail(q: JobQueue, msg: QueueMessage, reason: str) -> None:
    max_attempts = max(1, int(settings.worker_max_attempts or 3))
    if msg.attempt >= max_attempts:
        q.fail(msg, reason=reason[:500])
        logger.error("job dead-lettered job=%s attempts=%s reason=%s", msg.job_id[:8], msg.attempt, reason[:200])
    else:
        q.release(msg, delay_s=min(60.0, 2.0 ** msg.attempt))
        logger.info("job released for retry job=%s attempt=%s/%s", msg.job_id[:8], msg.attempt, max_attempts)


def run_worker_pool(
    *,
    concurrency: int | None = None,
    stop_event: threading.Event | None = None,
    idle_sleep_s: float = 0.5,
    target: str = DEFAULT_TARGET,
    job_timeout_s: float | None = None,
    queue: JobQueue | None = None,
) -> None:
    q = queue or get_job_queue()
    stop = stop_event or threading.Event()
    n = max(1, int(concurrency or settings.worker_concurrency or 1))
    timeout = float(job_timeout_s or settings.worker_job_timeout_s or 1800)
    ctx = mp.get_context("spawn")
    slots: dict[str, _Slot] = {}
    logger.info("worker pool starting concurrency=%s job_timeout_s=%s", n, int(timeout))

    def reap() -> None:
        now = time.time()
        for job_id, slot in list(slots.items()):
            if slot.proc.is_alive():
                if now - slot.started > timeout:
                    logger.error("job timed out after %ss job=%s — killing", int(timeout), job_id[:8])
                    slot.proc.terminate()
                    slot.proc.join(5)
                    if slot.proc.is_alive():
                        slot.proc.kill()
                        slot.proc.join(2)
                    slot.beat.set()
                    del slots[job_id]
                    _retry_or_fail(q, slot.msg, f"job_timeout_{int(timeout)}s")
                continue
            slot.proc.join()
            slot.beat.set()
            del slots[job_id]
            code = slot.proc.exitcode
            if code == 0:
                q.complete(slot.msg)
                logger.info("job done job=%s secs=%.1f", job_id[:8], now - slot.started)
            else:
                _retry_or_fail(q, slot.msg, f"child_exit_{code}")

    while not stop.is_set():
        reap()
        if len(slots) < n:
            msg = q.claim(visibility_timeout_s=VISIBILITY_TIMEOUT_S)
            if msg is not None:
                if msg.job_id in slots:
                    # Defensive: never run the same job twice in this instance.
                    q.release(msg, delay_s=5.0)
                    continue
                proc = ctx.Process(
                    target=_child_main,
                    args=(target, msg.to_json()),
                    name=f"job-{msg.job_id[:8]}",
                    daemon=False,
                )
                proc.start()
                slots[msg.job_id] = _Slot(msg=msg, proc=proc, beat=_start_heartbeat(q, msg), started=time.time())
                logger.info("job started job=%s attempt=%s running=%s/%s", msg.job_id[:8], msg.attempt, len(slots), n)
                continue  # try to fill another slot immediately
        stop.wait(idle_sleep_s)

    # Draining (SIGTERM): stop claiming; let running jobs finish while we can.
    deadline = time.time() + 25
    while slots and time.time() < deadline:
        reap()
        time.sleep(0.2)
    for slot in slots.values():
        # Not finished before shutdown: leave the claim to expire so another
        # worker re-claims it (~VISIBILITY_TIMEOUT_S), without burning an attempt.
        slot.beat.set()
    logger.info("worker pool stopped (unfinished=%s)", len(slots))
