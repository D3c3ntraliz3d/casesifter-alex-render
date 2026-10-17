"""Login, signup, and reset limits.

Redis when REDIS_URL is set so a restart or a second instance shares the window.
Memory only when Redis is unset (local tests). Production with a broken Redis
fails closed instead of allowing unlimited attempts.
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import HTTPException

_LOCK = threading.Lock()
_BUCKETS: dict[str, list[float]] = {}
_REDIS = None
_REDIS_URL = ""


def _production() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def _redis():
    global _REDIS, _REDIS_URL
    url = (os.environ.get("REDIS_URL") or "").strip()
    if not url:
        return None
    if _REDIS is not None and _REDIS_URL == url:
        return _REDIS
    import redis

    _REDIS = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
    _REDIS_URL = url
    return _REDIS


def reset_for_tests() -> None:
    global _REDIS, _REDIS_URL
    with _LOCK:
        _BUCKETS.clear()
    _REDIS = None
    _REDIS_URL = ""


def _memory_hit(key: str, *, limit: int, window_s: float) -> None:
    now = time.time()
    with _LOCK:
        bucket = [stamp for stamp in _BUCKETS.get(key, []) if now - stamp < window_s]
        if len(bucket) >= limit:
            _BUCKETS[key] = bucket
            raise HTTPException(429, "Too many attempts. Try again later.")
        bucket.append(now)
        _BUCKETS[key] = bucket


def enforce(key: str, *, limit: int = 10, window_s: float = 600.0) -> None:
    client = None
    try:
        client = _redis()
    except Exception as exc:
        if _production():
            raise HTTPException(503, "Rate limit store unavailable.") from exc
        client = None
    if client is None:
        _memory_hit(key, limit=limit, window_s=window_s)
        return
    redis_key = f"casesifter:rate:{key}"
    try:
        count = int(client.incr(redis_key))
        if count == 1:
            client.expire(redis_key, int(window_s))
    except Exception as exc:
        if _production():
            raise HTTPException(503, "Rate limit store unavailable.") from exc
        _memory_hit(key, limit=limit, window_s=window_s)
        return
    if count > limit:
        raise HTTPException(429, "Too many attempts. Try again later.")
