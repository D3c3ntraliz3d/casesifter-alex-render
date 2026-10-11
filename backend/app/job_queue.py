"""Scan job queue (local filesystem now; Cloud Tasks in Phase 3)."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class QueueMessage:
    job_id: str
    bundle_prefix: str
    attempt: int = 1
    enqueued_at: float = 0.0
    firm_id: str | None = None
    matter_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "QueueMessage":
        data = json.loads(raw)
        return cls(
            job_id=str(data["job_id"]),
            bundle_prefix=str(data["bundle_prefix"]),
            attempt=int(data.get("attempt") or 1),
            enqueued_at=float(data.get("enqueued_at") or 0.0),
            firm_id=data.get("firm_id"),
            matter_id=data.get("matter_id"),
        )


class JobQueue(ABC):
    @abstractmethod
    def enqueue(self, message: QueueMessage) -> None:
        ...

    @abstractmethod
    def claim(self, *, visibility_timeout_s: int = 900) -> QueueMessage | None:
        """Atomically claim one ready message, or None if idle."""

    @abstractmethod
    def complete(self, message: QueueMessage) -> None:
        """Ack success — remove from in-flight."""

    @abstractmethod
    def release(self, message: QueueMessage, *, delay_s: float = 0) -> None:
        """Return to ready queue for retry (possibly delayed)."""

    @abstractmethod
    def fail(self, message: QueueMessage, *, reason: str) -> None:
        """Dead-letter / abandon after max attempts."""


class LocalJobQueue(JobQueue):
    """Directory queue under ``settings.queue_dir`` (ready / inflight / dead)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else settings.queue_dir
        self.ready = self.root / "ready"
        self.inflight = self.root / "inflight"
        self.dead = self.root / "dead"
        for path in (self.ready, self.inflight, self.dead):
            path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _msg_path(self, folder: Path, message: QueueMessage) -> Path:
        return folder / f"{message.job_id}.json"

    def enqueue(self, message: QueueMessage) -> None:
        if not message.enqueued_at:
            message.enqueued_at = time.time()
        path = self._msg_path(self.ready, message)
        tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(message.to_json(), encoding="utf-8")
        tmp.replace(path)

    def claim(self, *, visibility_timeout_s: int = 900) -> QueueMessage | None:
        with self._lock:
            self._requeue_expired(visibility_timeout_s)
            candidates = sorted(self.ready.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in candidates:
                try:
                    msg = QueueMessage.from_json(path.read_text(encoding="utf-8"))
                except Exception:
                    path.unlink(missing_ok=True)
                    continue
                dest = self._msg_path(self.inflight, msg)
                try:
                    path.replace(dest)
                except OSError:
                    continue
                # Stamp claim time for visibility timeout.
                meta = dest.with_suffix(".claimed")
                meta.write_text(str(time.time()), encoding="utf-8")
                return msg
        return None

    def _requeue_expired(self, visibility_timeout_s: int) -> None:
        now = time.time()
        for path in list(self.inflight.glob("*.json")):
            stamp = path.with_suffix(".claimed")
            try:
                claimed_at = float(stamp.read_text(encoding="utf-8")) if stamp.is_file() else 0.0
            except Exception:
                claimed_at = 0.0
            if claimed_at and (now - claimed_at) < visibility_timeout_s:
                continue
            try:
                msg = QueueMessage.from_json(path.read_text(encoding="utf-8"))
            except Exception:
                path.unlink(missing_ok=True)
                stamp.unlink(missing_ok=True)
                continue
            path.replace(self._msg_path(self.ready, msg))
            stamp.unlink(missing_ok=True)

    def complete(self, message: QueueMessage) -> None:
        path = self._msg_path(self.inflight, message)
        path.unlink(missing_ok=True)
        path.with_suffix(".claimed").unlink(missing_ok=True)

    def release(self, message: QueueMessage, *, delay_s: float = 0) -> None:
        path = self._msg_path(self.inflight, message)
        path.with_suffix(".claimed").unlink(missing_ok=True)
        message.attempt = int(message.attempt) + 1
        if delay_s > 0:
            # Ready but mtime in the future via a delay sidecar.
            ready = self._msg_path(self.ready, message)
            ready.write_text(message.to_json(), encoding="utf-8")
            (ready.with_suffix(".delay")).write_text(str(time.time() + delay_s), encoding="utf-8")
            path.unlink(missing_ok=True)
        else:
            if path.is_file():
                path.replace(self._msg_path(self.ready, message))
            else:
                self.enqueue(message)

    def fail(self, message: QueueMessage, *, reason: str) -> None:
        path = self._msg_path(self.inflight, message)
        dead = self.dead / f"{message.job_id}.json"
        payload = asdict(message)
        payload["failed_reason"] = reason[:500]
        dead.write_text(json.dumps(payload), encoding="utf-8")
        path.unlink(missing_ok=True)
        path.with_suffix(".claimed").unlink(missing_ok=True)

    def ready_count(self) -> int:
        now = time.time()
        n = 0
        for path in self.ready.glob("*.json"):
            delay = path.with_suffix(".delay")
            if delay.is_file():
                try:
                    if float(delay.read_text(encoding="utf-8")) > now:
                        continue
                except Exception:
                    pass
                delay.unlink(missing_ok=True)
            n += 1
        return n


class CloudTasksQueue(JobQueue):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("Cloud Tasks queue lands in Phase 3.")


_queue: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _queue
    if _queue is not None:
        return _queue
    backend = (settings.queue_backend or "local").strip().lower()
    if backend == "cloudtasks":
        _queue = CloudTasksQueue()
    else:
        _queue = LocalJobQueue()
    return _queue


def reset_job_queue_for_tests(queue: JobQueue | None = None) -> None:
    global _queue
    _queue = queue


def run_local_worker_loop(
    handler: Callable[[QueueMessage], None],
    *,
    idle_sleep_s: float = 0.5,
    stop_event: threading.Event | None = None,
) -> None:
    """Poll the local queue forever (or until stop_event)."""
    q = get_job_queue()
    stop = stop_event or threading.Event()
    while not stop.is_set():
        msg = q.claim()
        if msg is None:
            stop.wait(idle_sleep_s)
            continue
        try:
            handler(msg)
            q.complete(msg)
        except Exception as exc:
            logger.exception("worker handler failed job=%s", msg.job_id[:8])
            max_attempts = max(1, int(settings.worker_max_attempts or 3))
            if msg.attempt >= max_attempts:
                q.fail(msg, reason=str(exc)[:500])
            else:
                q.release(msg, delay_s=min(60.0, 2.0 ** msg.attempt))
