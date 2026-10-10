"""P0-3: analysis mode mounts only /health as unauthenticated public API."""

from __future__ import annotations

import inspect
import os

os.environ["APP_MODE"] = "analysis"
os.environ.setdefault("FACT_MEMORY_MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LTEyMzQ1Njc4OTBhYmNkZWY=")

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


def _dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()

    def walk(dep) -> None:
        call = getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", "") or "")
        for child in getattr(dep, "dependencies", None) or []:
            walk(child)

    walk(route.dependant)
    # Also inspect signature defaults (Depends(...)).
    try:
        sig = inspect.signature(route.endpoint)
    except (TypeError, ValueError):
        return names
    for p in sig.parameters.values():
        default = p.default
        if default is inspect.Parameter.empty:
            continue
        dep = getattr(default, "dependency", None)
        if dep is not None:
            names.add(getattr(dep, "__name__", "") or "")
    return names


def test_analysis_mode_only_health_is_public():
    from app.main import app

    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/api/health" in paths
    assert "/api/entitlement" not in paths
    assert "/api/entitlement/license" not in paths

    v1_routes = [
        r for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/api/v1/")
    ]
    assert v1_routes, "expected /api/v1 routes"
    for route in v1_routes:
        names = {n.lower() for n in _dependency_names(route)}
        has_auth = any(
            "auth" in n or "require" in n or "firm" in n
            for n in names
        )
        assert has_auth, f"{route.path} [{','.join(sorted(route.methods))}] missing auth dependency: {names}"

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/entitlement").status_code == 404
        assert client.post("/api/entitlement/license", json={"license_key": "x"}).status_code == 404
