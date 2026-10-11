"""Google 503 ("model overloaded") must be retried, not treated as a failed review."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()

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
    monkeypatch.setattr(cl.settings, "gemini_api_key", "test")
    reset_ai_review_state()
    return cl


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
    assert post._post_gemini({"x": 1}) == {"ok": True}
    assert calls["n"] == 3
    assert get_ai_review_failure() is None


def test_persistent_503_fails_after_retries_as_error(post, monkeypatch):
    from app.gemini_review_state import get_ai_review_failure

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="503"):
        post._post_gemini({"x": 1})
    assert calls["n"] == 5
    assert get_ai_review_failure() == "error"


def test_persistent_429_still_reports_rate_limited(post, monkeypatch):
    from app.gemini_review_state import get_ai_review_failure

    monkeypatch.setattr(
        post.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(_http_error(429))
    )
    with pytest.raises(RuntimeError, match="429"):
        post._post_gemini({"x": 1})
    assert get_ai_review_failure() == "rate_limited"


def test_400_is_not_retried(post, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_error(400)

    monkeypatch.setattr(post.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="400"):
        post._post_gemini({"x": 1})
    assert calls["n"] == 1
