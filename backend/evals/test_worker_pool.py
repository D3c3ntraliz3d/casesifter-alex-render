"""Worker pool: several jobs at once, each in its own child process."""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.job_queue import LocalJobQueue, QueueMessage


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_pool(q, *, target, concurrency, until, timeout_s=None, max_wait=30):
    from worker.pool import run_worker_pool

    stop = threading.Event()
    t = threading.Thread(
        target=run_worker_pool,
        kwargs=dict(
            concurrency=concurrency,
            stop_event=stop,
            idle_sleep_s=0.05,
            target=target,
            job_timeout_s=timeout_s,
            queue=q,
        ),
        daemon=True,
    )
    t.start()
    deadline = time.time() + max_wait
    while time.time() < deadline and not until():
        time.sleep(0.1)
    stop.set()
    t.join(timeout=40)
    assert until(), "pool did not reach expected state in time"


@pytest.fixture()
def pool_env(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("POOL_TEST_LOG", str(log))
    q = LocalJobQueue(tmp_path / "queue")
    return q, log, tmp_path


def _enqueue(q, n, prefix="a"):
    ids = [f"{prefix}{i:031d}" for i in range(n)]
    for job_id in ids:
        q.enqueue(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}"))
    return ids


def test_pool_runs_jobs_concurrently_in_separate_processes(pool_env):
    q, log, tmp = pool_env
    ids = _enqueue(q, 3)
    done = lambda: sum(1 for e in _events(log) if e["event"] == "end") == 3  # noqa: E731
    t0 = time.time()
    _run_pool(q, target="evals._pool_targets:sleep_ok", concurrency=3, until=done)
    elapsed = time.time() - t0

    ev = _events(log)
    starts = [e for e in ev if e["event"] == "start"]
    assert sorted(e["job"] for e in starts) == sorted(ids)  # each job exactly once
    assert len({e["pid"] for e in starts}) == 3  # separate processes
    # All three overlapped: last start happened before first end.
    first_end = min(e["t"] for e in ev if e["event"] == "end")
    assert max(e["t"] for e in starts) < first_end
    assert elapsed < 20
    time.sleep(0.3)
    assert not list((tmp / "queue" / "inflight").glob("*.json"))
    assert not list((tmp / "queue" / "ready").glob("*.json"))


def test_pool_respects_concurrency_limit(pool_env):
    q, log, _ = pool_env
    _enqueue(q, 4, prefix="b")
    done = lambda: sum(1 for e in _events(log) if e["event"] == "end") == 4  # noqa: E731
    _run_pool(q, target="evals._pool_targets:sleep_ok", concurrency=2, until=done)
    ev = sorted(_events(log), key=lambda e: e["t"])
    running = peak = 0
    for e in ev:
        running += 1 if e["event"] == "start" else -1
        peak = max(peak, running)
    assert peak == 2


def test_pool_retries_then_dead_letters_failing_job(pool_env, monkeypatch):
    import worker.pool as pool_mod

    q, log, tmp = pool_env
    monkeypatch.setattr(pool_mod.settings, "worker_max_attempts", 3)
    # No backoff delay so the test runs fast.
    orig_release = q.release
    monkeypatch.setattr(q, "release", lambda m, delay_s=0: orig_release(m, delay_s=0))
    _enqueue(q, 1, prefix="c")
    dead = lambda: bool(list((tmp / "queue" / "dead").glob("*.json")))  # noqa: E731
    _run_pool(q, target="evals._pool_targets:always_fail", concurrency=2, until=dead)
    attempts = [e["attempt"] for e in _events(log) if e["event"] == "start"]
    assert attempts == [1, 2, 3]


def test_pool_kills_hung_job_and_retries(pool_env, monkeypatch):
    import worker.pool as pool_mod

    q, log, tmp = pool_env
    monkeypatch.setattr(pool_mod.settings, "worker_max_attempts", 1)
    _enqueue(q, 1, prefix="d")
    dead = lambda: bool(list((tmp / "queue" / "dead").glob("*.json")))  # noqa: E731
    t0 = time.time()
    _run_pool(q, target="evals._pool_targets:hang", concurrency=1, until=dead, timeout_s=2)
    assert time.time() - t0 < 20  # did not wait out the 60s hang
    reason = json.loads(next((tmp / "queue" / "dead").glob("*.json")).read_text())["failed_reason"]
    assert reason.startswith("job_timeout")
