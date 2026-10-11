"""Scan worker must validate TASK_* models on process start (same as analysis)."""

from __future__ import annotations


def test_worker_main_runs_startup_validate_and_probe(monkeypatch):
    import app.storage as storage_mod
    import worker.main as wm

    monkeypatch.setattr(storage_mod, "_report_fernet_override", None)
    monkeypatch.setattr("app.worker_auth.require_worker_token_configured", lambda: None)
    probed = {"n": 0}

    def _probe() -> None:
        probed["n"] += 1

    def _stop(*_a, **_k) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("app.gemini_tasks.startup_validate_and_probe", _probe)
    monkeypatch.setattr("worker.pool.run_worker_pool", _stop)

    wm.main()
    assert probed["n"] == 1
    assert storage_mod._report_fernet_override is not None
