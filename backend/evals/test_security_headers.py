"""Hardened API security headers (HSTS on HTTPS production)."""

from __future__ import annotations

from types import SimpleNamespace

from app.edge_security import HSTS_VALUE, apply_security_headers


class _Hdr(dict):
    def setdefault(self, key, value):
        if key not in self:
            self[key] = value
        return self[key]


def test_apply_security_headers_baseline():
    response = SimpleNamespace(headers=_Hdr())
    apply_security_headers(response, https=False, production=False)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_only_on_https_production():
    response = SimpleNamespace(headers=_Hdr())
    apply_security_headers(response, https=True, production=True)
    assert response.headers["Strict-Transport-Security"] == HSTS_VALUE
    assert "preload" in response.headers["Strict-Transport-Security"]


def test_no_hsts_on_http_even_in_production():
    response = SimpleNamespace(headers=_Hdr())
    apply_security_headers(response, https=False, production=True)
    assert "Strict-Transport-Security" not in response.headers
