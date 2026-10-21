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
    "alex": {
        "app": "https://casesifter-alex-app.onrender.com",
        "analysis": "https://casesifter-alex-analysis.onrender.com",
    },
    "john": {
        "app": "https://casesifter-app.onrender.com",
        "analysis": "https://casesifter-analysis.onrender.com",
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
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    status, login = _request(
        opener,
        f"{app}/v1/login",
        method="POST",
        body={"email": email, "password": password},
    )
    if status != 200 or not isinstance(login, dict) or not login.get("analysis_code"):
        _fail(f"login via app rewrite → {status} {login!r}")

    status, accept = _request(
        opener,
        f"{app}/api/v1/session/accept",
        method="POST",
        body={"code": login["analysis_code"]},
        headers={"Origin": app, "Referer": f"{app}/"},
    )
    if status != 200:
        _fail(f"session/accept → {status} {accept!r}")

    # Path A: normal browser Origin
    status, created = _request(
        opener,
        f"{app}/api/v1/matters",
        method="POST",
        body={"name": f"deploy-smoke-{stack}-origin"},
        headers={"Origin": app, "Referer": f"{app}/"},
    )
    if status != 200:
        _fail(f"create matter (Origin) → {status} {created!r}")
    if isinstance(created, dict) and "Cross-site" in str(created.get("detail", "")):
        _fail(f"create matter (Origin) CSRF: {created}")

    # Path B: missing Origin, allowlisted Referer (rewrite / CSRF regression)
    status, created2 = _request(
        opener,
        f"{app}/api/v1/matters",
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

    # Health tip (informational)
    status, health = _request(opener, f"{cfg['analysis']}/api/health")
    tip = health.get("git_commit_short") if isinstance(health, dict) else "?"
    print(f"OK stack={stack} analysis={tip} create=origin+referer")


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
