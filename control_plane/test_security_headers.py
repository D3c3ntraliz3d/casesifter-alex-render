"""Control-plane security header policy."""

from __future__ import annotations

from types import SimpleNamespace

from edge_security import HSTS_VALUE, apply_security_headers


class _Hdr(dict):
    def setdefault(self, key, value):
        if key not in self:
            self[key] = value
        return self[key]


def test_control_plane_hsts_on_https_production():
    response = SimpleNamespace(headers=_Hdr())
    apply_security_headers(response, https=True, production=True)
    assert response.headers["Strict-Transport-Security"] == HSTS_VALUE
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"


def test_control_plane_skips_hsts_on_cleartext():
    response = SimpleNamespace(headers=_Hdr())
    apply_security_headers(response, https=False, production=True)
    assert "Strict-Transport-Security" not in response.headers
