"""Scan job queue: local filesystem (single host) or Redis (shared, multi-worker)."""

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
    # Set by a claim on queues that track ownership (Redis). Never persisted.
    claim_token: str | None = None

    def to_json(self) -> str:
        data = asdict(self)
        data.pop("claim_token", None)
        return json.dumps(data, separators=(",", ":"))

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

    def heartbeat(self, message: QueueMessage) -> bool:
        """Extend the claim on an in-flight message. Returns False if no longer held."""
        return True

    def drop_ready(self, job_id: str) -> bool:
        """Remove a not-yet-claimed job from the ready queue. False if missing/in-flight."""
        return False


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

    @staticmethod
    def _write_stamp(stamp: Path) -> None:
        """Atomically write a claim stamp (a reader must never see an empty file)."""
        tmp = stamp.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(str(time.time()), encoding="utf-8")
        tmp.replace(stamp)

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
            now = time.time()
            candidates = sorted(self.ready.glob("*.json"), key=lambda p: p.stat().st_mtime)
            for path in candidates:
                delay = path.with_suffix(".delay")
                if delay.is_file():
                    try:
                        if float(delay.read_text(encoding="utf-8")) > now:
                            continue
                    except Exception:
                        pass
                    delay.unlink(missing_ok=True)
                try:
                    msg = QueueMessage.from_json(path.read_text(encoding="utf-8"))
                except Exception:
                    path.unlink(missing_ok=True)
                    continue
                dest = self._msg_path(self.inflight, msg)
                stamp = dest.with_suffix(".claimed")
                # Stamp before the move so an in-flight message is never stampless.
                self._write_stamp(stamp)
                try:
                    path.replace(dest)
                except OSError:
                    stamp.unlink(missing_ok=True)
                    continue
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

    def heartbeat(self, message: QueueMessage) -> bool:
        """Refresh the claim stamp so long jobs are not requeued mid-run."""
        with self._lock:
            path = self._msg_path(self.inflight, message)
            if not path.is_file():
                return False
            self._write_stamp(path.with_suffix(".claimed"))
            return True

    def complete(self, message: QueueMessage) -> None:
        path = self._msg_path(self.inflight, message)
        path.unlink(missing_ok=True)
        path.with_suffix(".claimed").unlink(missing_ok=True)

    def release(self, message: QueueMessage, *, delay_s: float = 0) -> None:
        with self._lock:
            path = self._msg_path(self.inflight, message)
            path.with_suffix(".claimed").unlink(missing_ok=True)
            message.attempt = int(message.attempt) + 1
            ready = self._msg_path(self.ready, message)
            # Write the delay sidecar BEFORE the message becomes visible so a
            # concurrent claim can never pick it up without backoff.
            delay = ready.with_suffix(".delay")
            if delay_s > 0:
                delay.write_text(str(time.time() + delay_s), encoding="utf-8")
            else:
                delay.unlink(missing_ok=True)
            # Atomic write: always rewrite JSON so the bumped attempt is persisted.
            tmp = ready.with_suffix(f".{uuid.uuid4().hex}.tmp")
            tmp.write_text(message.to_json(), encoding="utf-8")
            tmp.replace(ready)
            path.unlink(missing_ok=True)

    def fail(self, message: QueueMessage, *, reason: str) -> None:
        path = self._msg_path(self.inflight, message)
        dead = self.dead / f"{message.job_id}.json"
        payload = json.loads(message.to_json())
        payload["failed_reason"] = reason[:500]
        dead.write_text(json.dumps(payload), encoding="utf-8")
        path.unlink(missing_ok=True)
        path.with_suffix(".claimed").unlink(missing_ok=True)

    def drop_ready(self, job_id: str) -> bool:
        """Drop a queued message that has not been claimed yet."""
        jid = (job_id or "").strip()
        if not jid:
            return False
        with self._lock:
            inflight = self.inflight / f"{jid}.json"
            if inflight.is_file():
                return False
            ready = self.ready / f"{jid}.json"
            delay = self.ready / f"{jid}.delay"
            if not ready.is_file() and not delay.is_file():
                return False
            ready.unlink(missing_ok=True)
            delay.unlink(missing_ok=True)
            return True

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


_CLAIM_LUA = """
local ready, inflight, msgs, claims = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local now, vis, token = tonumber(ARGV[1]), tonumber(ARGV[2]), ARGV[3]
-- Requeue claims whose holder stopped heartbeating.
local expired = redis.call('ZRANGEBYSCORE', inflight, '-inf', now - vis)
for _, id in ipairs(expired) do
  redis.call('ZREM', inflight, id)
  redis.call('HDEL', claims, id)
  redis.call('ZADD', ready, now, id)
end
local ids = redis.call('ZRANGEBYSCORE', ready, '-inf', now, 'LIMIT', 0, 1)
if #ids == 0 then return false end
local id = ids[1]
redis.call('ZREM', ready, id)
local body = redis.call('HGET', msgs, id)
if not body then return false end
redis.call('ZADD', inflight, now, id)
redis.call('HSET', claims, id, token)
return body
"""

_ENQUEUE_LUA = """
local ready, inflight, msgs = KEYS[1], KEYS[2], KEYS[3]
local id, body, at = ARGV[1], ARGV[2], tonumber(ARGV[3])
-- Never disturb a job a worker currently holds (boot recovery re-enqueues).
if redis.call('ZSCORE', inflight, id) then return 0 end
redis.call('HSET', msgs, id, body)
redis.call('ZADD', ready, at, id)
return 1
"""

_HEARTBEAT_LUA = """
local inflight, claims = KEYS[1], KEYS[2]
local id, token, now = ARGV[1], ARGV[2], tonumber(ARGV[3])
if redis.call('HGET', claims, id) ~= token then return 0 end
redis.call('ZADD', inflight, 'XX', now, id)
return 1
"""

_COMPLETE_LUA = """
local inflight, claims, msgs = KEYS[1], KEYS[2], KEYS[3]
local id, token = ARGV[1], ARGV[2]
if redis.call('HGET', claims, id) ~= token then return 0 end
redis.call('ZREM', inflight, id)
redis.call('HDEL', claims, id)
redis.call('HDEL', msgs, id)
return 1
"""

_RELEASE_LUA = """
local ready, inflight, claims, msgs = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local id, token, body, at = ARGV[1], ARGV[2], ARGV[3], tonumber(ARGV[4])
if redis.call('HGET', claims, id) ~= token then return 0 end
redis.call('ZREM', inflight, id)
redis.call('HDEL', claims, id)
redis.call('HSET', msgs, id, body)
redis.call('ZADD', ready, at, id)
return 1
"""

_FAIL_LUA = """
local inflight, claims, msgs, dead = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local id, token, body = ARGV[1], ARGV[2], ARGV[3]
if redis.call('HGET', claims, id) ~= token then return 0 end
redis.call('ZREM', inflight, id)
redis.call('HDEL', claims, id)
redis.call('HDEL', msgs, id)
redis.call('HSET', dead, id, body)
return 1
"""

_DROP_READY_LUA = """
local ready, inflight, msgs = KEYS[1], KEYS[2], KEYS[3]
local id = ARGV[1]
if redis.call('ZSCORE', inflight, id) then return 0 end
local removed = redis.call('ZREM', ready, id)
redis.call('HDEL', msgs, id)
return removed
"""


class RedisJobQueue(JobQueue):
    """Shared queue for many worker instances (Render Key Value or any Redis).

    Every claim gets a random token; complete/release/fail/heartbeat only act if
    the caller still holds that token. A worker whose claim expired and was
    re-claimed elsewhere can therefore never delete or requeue the new holder's
    job. All state transitions are single Lua scripts (atomic).
    """

    def __init__(self, client=None, prefix: str | None = None) -> None:
        if client is None:
            import redis

            url = (settings.redis_url or "").strip()
            if not url:
                raise RuntimeError("REDIS_URL is required when QUEUE_BACKEND=redis")
            client = redis.Redis.from_url(url, decode_responses=True, health_check_interval=30)
        self.r = client
        p = (prefix or settings.redis_queue_prefix or "casesifter:scan").rstrip(":")
        self.k_ready = f"{p}:ready"
        self.k_inflight = f"{p}:inflight"
        self.k_msgs = f"{p}:msgs"
        self.k_claims = f"{p}:claims"
        self.k_dead = f"{p}:dead"
        self._claim = self.r.register_script(_CLAIM_LUA)
        self._enqueue = self.r.register_script(_ENQUEUE_LUA)
        self._heartbeat = self.r.register_script(_HEARTBEAT_LUA)
        self._complete = self.r.register_script(_COMPLETE_LUA)
        self._release = self.r.register_script(_RELEASE_LUA)
        self._fail = self.r.register_script(_FAIL_LUA)
        self._drop_ready = self.r.register_script(_DROP_READY_LUA)

    def enqueue(self, message: QueueMessage) -> None:
        if not message.enqueued_at:
            message.enqueued_at = time.time()
        added = self._enqueue(
            keys=[self.k_ready, self.k_inflight, self.k_msgs],
            args=[message.job_id, message.to_json(), time.time()],
        )
        if not added:
            logger.info("enqueue skipped — job in flight job=%s", message.job_id[:8])

    def claim(self, *, visibility_timeout_s: int = 900) -> QueueMessage | None:
        token = uuid.uuid4().hex
        body = self._claim(
            keys=[self.k_ready, self.k_inflight, self.k_msgs, self.k_claims],
            args=[time.time(), int(visibility_timeout_s), token],
        )
        if not body:
            return None
        msg = QueueMessage.from_json(body)
        msg.claim_token = token
        return msg

    def heartbeat(self, message: QueueMessage) -> bool:
        return bool(
            self._heartbeat(
                keys=[self.k_inflight, self.k_claims],
                args=[message.job_id, message.claim_token or "", time.time()],
            )
        )

    def complete(self, message: QueueMessage) -> None:
        ok = self._complete(
            keys=[self.k_inflight, self.k_claims, self.k_msgs],
            args=[message.job_id, message.claim_token or ""],
        )
        if not ok:
            logger.warning("complete ignored — claim lost job=%s", message.job_id[:8])

    def release(self, message: QueueMessage, *, delay_s: float = 0) -> None:
        nxt = QueueMessage.from_json(message.to_json())
        nxt.attempt = int(message.attempt) + 1
        ok = self._release(
            keys=[self.k_ready, self.k_inflight, self.k_claims, self.k_msgs],
            args=[message.job_id, message.claim_token or "", nxt.to_json(), time.time() + max(0.0, delay_s)],
        )
        if ok:
            message.attempt = nxt.attempt
        else:
            logger.warning("release ignored — claim lost job=%s", message.job_id[:8])

    def fail(self, message: QueueMessage, *, reason: str) -> None:
        payload = json.loads(message.to_json())
        payload["failed_reason"] = reason[:500]
        payload["failed_at"] = time.time()
        ok = self._fail(
            keys=[self.k_inflight, self.k_claims, self.k_msgs, self.k_dead],
            args=[message.job_id, message.claim_token or "", json.dumps(payload)],
        )
        if not ok:
            logger.warning("fail ignored — claim lost job=%s", message.job_id[:8])

    def drop_ready(self, job_id: str) -> bool:
        jid = (job_id or "").strip()
        if not jid:
            return False
        return bool(
            self._drop_ready(
                keys=[self.k_ready, self.k_inflight, self.k_msgs],
                args=[jid],
            )
        )

    def ready_count(self) -> int:
        return int(self.r.zcount(self.k_ready, "-inf", time.time()))

    def inflight_count(self) -> int:
        return int(self.r.zcard(self.k_inflight))

    def dead_count(self) -> int:
        return int(self.r.hlen(self.k_dead))


_queue: JobQueue | None = None


def get_job_queue() -> JobQueue:
    global _queue
    if _queue is not None:
        return _queue
    backend = (settings.queue_backend or "local").strip().lower()
    if backend == "redis":
        _queue = RedisJobQueue()
    else:
        _queue = LocalJobQueue()
    return _queue


def reset_job_queue_for_tests(queue: JobQueue | None = None) -> None:
    global _queue
    _queue = queue


# One in-flight job per worker process (local loop is sequential; lock is the guard).
_process_job_lock = threading.Lock()

# Renew the claim well inside the visibility timeout (default 900s) so a long
# scan is never handed to a second worker while the first is still running.
HEARTBEAT_INTERVAL_S = 60.0
# With heartbeats, a short visibility timeout is safe: a crashed or redeployed
# worker's job is picked up by another worker in ~3 minutes instead of 15.
VISIBILITY_TIMEOUT_S = 180


def _start_heartbeat(q: JobQueue, msg: QueueMessage) -> threading.Event:
    done = threading.Event()

    def _beat() -> None:
        while not done.wait(HEARTBEAT_INTERVAL_S):
            try:
                if not q.heartbeat(msg):
                    logger.warning("heartbeat lost claim job=%s", msg.job_id[:8])
                    return
            except Exception:
                logger.warning("heartbeat failed job=%s", msg.job_id[:8], exc_info=True)

    threading.Thread(target=_beat, daemon=True, name=f"hb-{msg.job_id[:8]}").start()
    return done


def run_local_worker_loop(
    handler: Callable[[QueueMessage], None],
    *,
    idle_sleep_s: float = 0.5,
    stop_event: threading.Event | None = None,
) -> None:
    """Poll the local queue forever (or until stop_event). At most one job at a time."""
    q = get_job_queue()
    stop = stop_event or threading.Event()
    while not stop.is_set():
        msg = q.claim(visibility_timeout_s=VISIBILITY_TIMEOUT_S)
        if msg is None:
            stop.wait(idle_sleep_s)
            continue
        if not _process_job_lock.acquire(blocking=False):
            logger.error(
                "refusing concurrent job in same worker process job=%s",
                msg.job_id[:8],
            )
            q.release(msg, delay_s=1.0)
            continue
        beat = _start_heartbeat(q, msg)
        try:
            handler(msg)
            beat.set()
            q.complete(msg)
        except Exception as exc:
            beat.set()
            logger.exception("worker handler failed job=%s", msg.job_id[:8])
            max_attempts = max(1, int(settings.worker_max_attempts or 3))
            if msg.attempt >= max_attempts:
                q.fail(msg, reason=str(exc)[:500])
            else:
                q.release(msg, delay_s=min(60.0, 2.0 ** msg.attempt))
        finally:
            beat.set()
            _process_job_lock.release()
