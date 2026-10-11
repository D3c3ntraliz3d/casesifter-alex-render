"""P1: Gemini ahead leases must not stick after success, error, or timeout.

Production (Oct 11): ahead == GEMINI_MAX_CONCURRENT with inflight_mb=0.0 for
~10 minutes until restart — the in-memory ahead set leaked when stage exit
paths skipped clear_ahead.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest


@pytest.fixture(autouse=True)
def _clean_gemini_executor():
    from app.gemini_executor import reset_ahead_state_for_tests

    reset_ahead_state_for_tests()
    yield
    reset_ahead_state_for_tests()


def _patch_stage_deps(monkeypatch, *, timeout_s: float = 0.4, max_concurrent: int = 4):
    from app import config as config_mod
    from app import gemini_executor as ge

    monkeypatch.setattr(config_mod.settings, "gemini_max_concurrent", max_concurrent)
    monkeypatch.setattr(config_mod.settings, "gemini_stage_timeout_s", max(60, int(timeout_s)))
    monkeypatch.setattr(config_mod.settings, "gemini_orphan_source_grace_s", 5)
    # Production floors timeout at 60s; tests need a short budget.
    monkeypatch.setattr(ge, "_stage_timeout_s", lambda: float(timeout_s))
    monkeypatch.setattr(ge, "_orphan_grace_s", lambda: 5.0)
    monkeypatch.setattr(ge, "_max_ahead", lambda: max(1, int(max_concurrent)))

    monkeypatch.setattr("app.gemini_cancel.clear_cancel", lambda _jid: None)
    monkeypatch.setattr("app.gemini_cancel.request_cancel", lambda _jid: None)
    monkeypatch.setattr("app.gemini_cancel.set_current_job", lambda _jid: None)
    monkeypatch.setattr("app.scan_watchdog.note_queue_progress", lambda: None)
    monkeypatch.setattr("app.storage.load_progress", lambda _jid: None)
    monkeypatch.setattr("app.storage.update_job", lambda *_a, **_k: None)
    monkeypatch.setattr("app.v1_analysis._update_durable_job", lambda *_a, **_k: None)
    monkeypatch.setattr("app.ephemeral.delete_ephemeral_source", lambda *_a, **_k: None)


def test_ahead_returns_to_zero_after_mixed_gemini_jobs(monkeypatch):
    """~20 jobs including one error and one timeout → ahead == 0; new reserve works."""
    from app import gemini_executor as ge

    _patch_stage_deps(monkeypatch, timeout_s=0.35, max_concurrent=4)

    error_id = "job-error-0001"
    timeout_id = "job-timeout-0001"
    hang = threading.Event()

    def fake_stage(payload):
        jid = str(payload.get("job_id") or "")
        if jid == error_id:
            raise RuntimeError("simulated gemini crash")
        if jid == timeout_id:
            hang.wait(timeout=30)
            return {"job_id": jid, "status": "complete", "timing": {"total_s": 30}}
        time.sleep(0.02)
        return {
            "job_id": jid,
            "status": "complete",
            "timing": {"total_s": 0.02, "local_s": 0.01, "gemini_s": 0.01},
        }

    monkeypatch.setattr(
        "app.scan_job_runner.execute_gemini_stage",
        fake_stage,
    )

    outs: list[Future] = []
    for i in range(20):
        if i == 3:
            jid = error_id
        elif i == 7:
            jid = timeout_id
        else:
            jid = f"job-ok-{i:04d}"
        outs.append(ge.submit_gemini_stage({"job_id": jid}))

    # Wait for every outer future (timeout path returns a failed result, not exception).
    deadline = time.monotonic() + 15
    for fut in outs:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "jobs did not finish in time"
        try:
            fut.result(timeout=remaining)
        except Exception:
            # Error job may surface via set_exception.
            pass

    # Orphan timeout thread may still be in hang.wait; lease must already be free.
    assert ge.ahead_count() == 0, f"ahead leaked: {ge.ahead_count()}"

    # New work must be able to acquire a full set of slots.
    acquired = []
    for i in range(4):
        jid = f"job-reacquire-{i}"
        assert ge.reserve_ahead(jid, timeout=2.0), f"could not reserve after drain ({i})"
        acquired.append(jid)
    assert ge.ahead_count() == 4
    for jid in acquired:
        ge.clear_ahead(jid)
    assert ge.ahead_count() == 0

    hang.set()


def test_local_reserve_released_when_not_handed_to_gemini(monkeypatch):
    """reserve_ahead without Gemini handoff must clear via explicit release path."""
    from app import gemini_executor as ge

    _patch_stage_deps(monkeypatch, max_concurrent=2)
    assert ge.reserve_ahead("local-only-1", timeout=1.0)
    assert ge.ahead_count() == 1
    ge.clear_ahead("local-only-1")
    assert ge.ahead_count() == 0
    assert ge.reserve_ahead("local-only-2", timeout=1.0)
    ge.clear_ahead("local-only-2")
