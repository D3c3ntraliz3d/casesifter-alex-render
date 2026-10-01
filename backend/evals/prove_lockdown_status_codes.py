"""Prove Phase 0 lockdown status codes (vault before → analysis after).

Does not touch the suspended Render service. Local APP_MODE=vault is the
before baseline; APP_MODE=analysis (and RENDER=true) is the after target.
"""

from __future__ import annotations

import json
import os
import sys
from importlib import reload


def _probe(label: str, env: dict[str, str]) -> dict:
    for key in ("APP_MODE", "APP_ENV", "ANALYSIS_API_KEYS", "CORS_ALLOW_ORIGINS", "RENDER"):
        os.environ.pop(key, None)
    os.environ.update(env)

    import app.config as config_mod

    reload(config_mod)
    import app.api as api_mod

    reload(api_mod)
    import app.v1_analysis as v1_mod

    reload(v1_mod)
    import app.main as main_mod

    reload(main_mod)

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    health = client.get("/api/health")
    cases = client.get("/api/cases")
    v1 = client.post("/api/v1/jobs")
    row = {
        "label": label,
        "health": health.status_code,
        "health_mode": (health.json() or {}).get("app_mode") if health.status_code == 200 else None,
        "cases": cases.status_code,
        "v1_jobs": v1.status_code,
    }
    print(json.dumps(row, sort_keys=True))
    return row


def main() -> int:
    before = _probe(
        "before_vault",
        {
            "APP_MODE": "vault",
            "APP_ENV": "development",
            "CORS_ALLOW_ORIGINS": "*",
        },
    )
    after = _probe(
        "after_analysis",
        {
            "APP_MODE": "analysis",
            "APP_ENV": "production",
            "ANALYSIS_API_KEYS": "launch-key",
            "CORS_ALLOW_ORIGINS": "https://casesifter.com",
        },
    )
    render_default = _probe(
        "after_render_env",
        {
            "RENDER": "true",
            "APP_ENV": "production",
            "ANALYSIS_API_KEYS": "launch-key",
            "CORS_ALLOW_ORIGINS": "https://casesifter.com",
        },
    )

    ok = (
        before["health"] == 200
        and before["cases"] == 200
        and before["v1_jobs"] in {401, 402, 422}  # vault may open-entitlement to 422 missing file
        and after["health"] == 200
        and after["health_mode"] == "analysis"
        and after["cases"] == 404
        and after["v1_jobs"] == 401  # wrong/missing key → still 401 when key required; empty body hits auth first
        and render_default["health_mode"] == "analysis"
        and render_default["cases"] == 404
        and render_default["v1_jobs"] == 401
    )
    # After with valid key still needs a file → 422 once auth passes.
    for key in ("APP_MODE", "APP_ENV", "ANALYSIS_API_KEYS", "CORS_ALLOW_ORIGINS", "RENDER"):
        os.environ.pop(key, None)
    os.environ.update(
        {
            "APP_MODE": "analysis",
            "APP_ENV": "production",
            "ANALYSIS_API_KEYS": "launch-key",
            "CORS_ALLOW_ORIGINS": "https://casesifter.com",
        }
    )
    import app.config as config_mod

    reload(config_mod)
    import app.api as api_mod

    reload(api_mod)
    import app.v1_analysis as v1_mod

    reload(v1_mod)
    import app.main as main_mod

    reload(main_mod)
    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    authed = client.post("/api/v1/jobs", headers={"X-API-Key": "launch-key"})
    print(json.dumps({"label": "after_analysis_authed_no_file", "v1_jobs": authed.status_code}))
    ok = ok and authed.status_code == 422

    no_key = client.post("/api/v1/jobs")
    print(json.dumps({"label": "after_analysis_no_key", "v1_jobs": no_key.status_code}))
    ok = ok and no_key.status_code == 401

    print("LOCKDOWN", "all_pass" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
