"""Google 503 ("model overloaded") must be retried, not treated as a failed review."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest


class _Resp:
    def __init__(self, body: dict, headers: dict | None = None):
        self._b = json.dumps(body).encode()
        self.headers = headers or {}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code: int):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"overloaded"))


@pytest.fixture
def post(monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    monkeypatch.setattr(cl.time, "sleep", lambda s: None)
    monkeypatch.setattr(cl.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(cl.settings, "gemini_api_key", "test")
    monkeypatch.setattr(cl.settings, "task_a_extract_primary", "gemini-3.6-flash")
    monkeypatch.setattr(cl.settings, "task_a_extract_backup", "gemini-3.5-flash-lite")
    monkeypatch.setattr(cl.settings, "task_a_extract_thinking_level", "minimal")
    monkeypatch.setattr(cl.settings, "gemini_service_tier", "standard")
    monkeypatch.setattr(cl, "_notify_gemini_busy", lambda _jid: None)
    reset_ai_review_state()
    return cl


def _call(post, **kwargs):
    from app.gemini_tasks import TASK_A_EXTRACT

    return post._post_gemini({"x": 1}, task=TASK_A_EXTRACT, step="test", chunk_id="c1", **kwargs)


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_transient_5xx_is_retried_then_succeeds(post, monkeypatch, code):
    from app.gemini_review_state import get_ai_review_failure

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(code)
        return _Resp({"ok": True})

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    assert _call(post) == {"ok": True}
    assert calls["n"] == 3
    assert get_ai_review_failure() is None


def test_persistent_503_fails_after_primary_and_backup(post, monkeypatch):
    from app.gemini_review_state import get_ai_review_failure

    calls = {"n": 0}
    models: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        full = getattr(req, "full_url", str(req))
        models.append(full)
        raise _http_error(503)

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="503"):
        _call(post)
    # 4 primary + 3 backup
    assert calls["n"] == 7
    assert sum(1 for m in models if "gemini-3.6-flash" in m) == 4
    assert sum(1 for m in models if "gemini-3.5-flash-lite" in m) == 3
    assert get_ai_review_failure() == "error"


def test_persistent_429_still_reports_rate_limited(post, monkeypatch):
    from app.gemini_review_state import get_ai_review_failure

    monkeypatch.setattr(
        post.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429))
    )
    with pytest.raises(RuntimeError, match="429"):
        _call(post)
    assert get_ai_review_failure() == "rate_limited"


def test_400_is_not_retried(post, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(400)

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="400"):
        _call(post)
    assert calls["n"] == 1


def test_three_primary_retries_then_backup(post, monkeypatch):
    models: list[str] = []

    def fake_urlopen(req, timeout=None):
        full = getattr(req, "full_url", str(req))
        models.append(full)
        if len(models) < 5:
            raise _http_error(503)
        return _Resp({"ok": True})

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    assert _call(post) == {"ok": True}
    assert len(models) == 5
    assert all("gemini-3.6-flash" in m for m in models[:4])
    assert "gemini-3.5-flash-lite" in models[4]


def test_retry_delay_primary_targets_20_30s_band(post, monkeypatch):
    monkeypatch.setattr(post.random, "uniform", lambda a, b: 1.0)
    d0 = post.gemini_retry_delay(0, {}, code=503)
    d1 = post.gemini_retry_delay(1, {}, code=503)
    d2 = post.gemini_retry_delay(2, {}, code=503)
    assert 5.0 <= d0 <= 10.0
    assert 8.0 <= d1 <= 14.0
    assert 12.0 <= d2 <= 20.0
    assert 20.0 <= (d0 + d1 + d2) <= 35.0


def test_busy_message_posted_during_503_retry(post, monkeypatch):
    from app.gemini_cancel import set_current_job

    notes: list[str] = []

    def note(jid):
        notes.append(jid)

    monkeypatch.setattr(post, "_notify_gemini_busy", note)
    set_current_job("abcd" * 8)
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(503)
        return _Resp({"ok": True})

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    assert _call(post) == {"ok": True}
    assert notes
    set_current_job(None)


def test_task_a_priority_tier_in_request_body(post, monkeypatch):
    monkeypatch.setattr(post.settings, "gemini_service_tier", "priority")
    seen: list[dict] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        seen.append(body)
        return _Resp({"ok": True}, headers={"x-gemini-service-tier": "priority"})

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    assert _call(post) == {"ok": True}
    assert seen[0].get("service_tier") == "priority"


def test_served_tier_header_recorded_even_when_downgraded(post, monkeypatch, caplog):
    import logging

    from app import gemini_timing

    monkeypatch.setattr(post.settings, "gemini_service_tier", "priority")
    gemini_timing.reset()

    def fake_urlopen(req, timeout=None):
        return _Resp({"ok": True}, headers={"x-gemini-service-tier": "standard"})

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    with caplog.at_level(logging.INFO):
        assert _call(post) == {"ok": True}
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "requested_tier=priority" in joined
    assert "served_tier=standard" in joined
    snap = gemini_timing.snapshot()
    assert snap["calls"] == 1
    assert snap["by_task_model_tier"][0]["served_tier"] == "standard"


def test_scan_summary_counts_503_and_fallback(post, monkeypatch):
    from app import gemini_timing

    gemini_timing.reset()
    models: list[str] = []

    def fake_urlopen(req, timeout=None):
        full = getattr(req, "full_url", str(req))
        models.append(full)
        if len(models) < 5:
            raise _http_error(503)
        return _Resp({"ok": True}, headers={"x-gemini-service-tier": "standard"})

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    assert _call(post) == {"ok": True}
    snap = gemini_timing.snapshot()
    assert snap["http_503"] == 4
    assert snap["fallback_count"] == 1
    assert snap["calls"] == 1


def test_no_gemini_20_flash_in_defaults():
    from app.gemini_tasks import all_task_configs

    for cfg in all_task_configs():
        assert "2.0-flash" not in cfg.primary
        assert "2.0-flash" not in cfg.backup
