#!/usr/bin/env python3
"""Post-deploy smoke: create a matter through the app same-origin rewrite.

Catches the regression where cookie sessions hit analysis CSRF with a missing
Origin (Render rewrite) and return ``Cross-site request blocked.``

Usage:
  python scripts/deploy_smoke_create_case.py --stack alex
  python scripts/deploy_smoke_create_case.py --stack john

Env:
  CASESIFTER_SMOKE_EMAIL / CASESIFTER_SMOKE_PASSWORD
  (defaults to demo john@ when unset — override in CI secrets)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar


STACKS = {
    # Alex: same-origin /v1 and /api rewrites on the static app (first-party cookies).
    "alex": {
        "mode": "same_origin",
        "app": "https://casesifter-alex-app.onrender.com",
        "analysis": "https://casesifter-alex-analysis.onrender.com",
        "control": "https://casesifter-alex-control.onrender.com",
    },
    # John prod: same-origin rewrites (aligned with alex after render.yaml update).
    "john": {
        "mode": "same_origin",
        "app": "https://casesifter-app.onrender.com",
        "analysis": "https://casesifter-analysis.onrender.com",
        "control": "https://casesifter-control.onrender.com",
    },
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
) -> tuple[int, dict | str]:
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener.open(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return int(exc.code), payload


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(stack: str, email: str, password: str) -> None:
    cfg = STACKS[stack]
    app = cfg["app"].rstrip("/")
    analysis = cfg["analysis"].rstrip("/")
    control = cfg["control"].rstrip("/")
    same_origin = cfg.get("mode") == "same_origin"
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login_url = f"{app}/v1/login" if same_origin else f"{control}/v1/login"
    status, login = _request(
        opener,
        login_url,
        method="POST",
        body={"email": email, "password": password},
        headers={"Origin": app} if not same_origin else None,
    )
    if status != 200 or not isinstance(login, dict) or not login.get("analysis_code"):
        _fail(f"login → {status} {login!r}")

    accept_url = (
        f"{app}/api/v1/session/accept" if same_origin else f"{analysis}/api/v1/session/accept"
    )
    status, accept = _request(
        opener,
        accept_url,
        method="POST",
        body={"code": login["analysis_code"]},
        headers={"Origin": app, "Referer": f"{app}/"},
    )
    if status != 200:
        _fail(f"session/accept → {status} {accept!r}")

    matters_url = f"{app}/api/v1/matters" if same_origin else f"{analysis}/api/v1/matters"

    # Path A: normal browser Origin
    status, created = _request(
        opener,
        matters_url,
        method="POST",
        body={"name": f"deploy-smoke-{stack}-origin"},
        headers={"Origin": app, "Referer": f"{app}/"},
    )
    if status != 200:
        _fail(f"create matter (Origin) → {status} {created!r}")
    if isinstance(created, dict) and "Cross-site" in str(created.get("detail", "")):
        _fail(f"create matter (Origin) CSRF: {created}")

    # Path B: missing Origin, allowlisted Referer (same-origin rewrite CSRF regression)
    status, created2 = _request(
        opener,
        matters_url,
        method="POST",
        body={"name": f"deploy-smoke-{stack}-referer"},
        headers={"Referer": f"{app}/"},
    )
    if status == 403 and isinstance(created2, dict) and "Cross-site" in str(
        created2.get("detail", "")
    ):
        _fail(
            "create matter (Referer-only) hit CSRF — same-origin rewrite Origin strip regression"
        )
    if status != 200:
        _fail(f"create matter (Referer-only) → {status} {created2!r}")

    status, health = _request(opener, f"{analysis}/api/health")
    tip = health.get("git_commit_short") if isinstance(health, dict) else "?"
    mode = "same_origin" if same_origin else "cross_origin"
    print(f"OK stack={stack} mode={mode} analysis={tip} create=origin+referer")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=sorted(STACKS), default="alex")
    parser.add_argument(
        "--email",
        default=os.environ.get("CASESIFTER_SMOKE_EMAIL", "john@casesifter.dev"),
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("CASESIFTER_SMOKE_PASSWORD", "CaseSifter-Demo-2026!"),
    )
    args = parser.parse_args()
    run(args.stack, args.email, args.password)


if __name__ == "__main__":
    main()
