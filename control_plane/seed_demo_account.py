"""Upsert demo firm owners with known credentials (local SQLite or remote DB).

Run from the control_plane directory:

    python seed_demo_account.py
    python seed_demo_account.py --remote https://casesifter-alex-control.onrender.com

Local mode uses CONTROL_DATA_DIR / SQLite (or DATABASE_URL if set).

Remote mode signs up via POST /v1/signup then grants trials via POST /v1/trial
(requires CONTROL_ADMIN_TOKEN and CASESIFTER_REQUIRE_EMAIL_VERIFY=false on the host).

Does not print other users' emails. Password is for local/demo only — not a
production secret. Set CASESIFTER_DEMO_PASSWORD to override the default.
Set CASESIFTER_ALLOW_DEMO_SEED=true when APP_ENV=production (local DB upsert).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

# Allow `python seed_demo_account.py` from control_plane/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Use a deliverable-looking domain (.local is rejected by EmailStr / email-validator).
DEMO_PASSWORD = (
    os.environ.get("CASESIFTER_DEMO_PASSWORD") or "CaseSifter-Demo-2026!"
).strip()
TRIAL_DAYS = int(os.environ.get("CASESIFTER_TRIAL_DAYS", "14") or 14)

# Separate firms so multi-tenant isolation is testable.
DEMO_ACCOUNTS: list[dict[str, str]] = [
    {
        "email": "john@casesifter.dev",
        "name": "John Firm",
        "password": DEMO_PASSWORD,
    },
    {
        "email": "alex@casesifter.dev",
        "name": "Alex Firm",
        "password": DEMO_PASSWORD,
    },
    {
        "email": "demo1@casesifter.dev",
        "name": "Demo1 Firm",
        "password": DEMO_PASSWORD,
    },
    {
        "email": "demo2@casesifter.dev",
        "name": "Demo2 Firm",
        "password": DEMO_PASSWORD,
    },
]


def _allow_production_seed() -> bool:
    return (os.environ.get("CASESIFTER_ALLOW_DEMO_SEED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _guard_production() -> int | None:
    if (os.environ.get("APP_ENV") or "").strip().lower() != "production":
        return None
    if _allow_production_seed():
        return None
    print(
        "Refusing to seed demo account while APP_ENV=production "
        "(set CASESIFTER_ALLOW_DEMO_SEED=true to override).",
        file=sys.stderr,
    )
    return 2


def _upsert_local(accounts: list[dict[str, str]]) -> list[tuple[str, str, str, str]]:
    import auth_store as auth
    from sqlalchemy import select

    auth.init_auth_store()
    trial_until = time.time() + TRIAL_DAYS * 86400
    results: list[tuple[str, str, str, str]] = []
    with auth.session_scope() as s:
        for acct in accounts:
            email = acct["email"].strip().lower()
            name = acct["name"].strip()
            password = acct["password"]
            row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
            if row is None:
                uid = uuid.uuid4().hex
                row = auth.CpUser(
                    id=uid,
                    email=email,
                    name=name,
                    password_hash=auth.hash_password(password),
                    password_algo="argon2id",
                    email_verified=True,
                    created_at=time.time(),
                    accepted_terms_at=time.time(),
                    trial_until=trial_until,
                    subscription_json='{"status":"trialing","plan":"pro","seats":1}',
                    cohort="demo",
                    support_code=f"CS-DEMO-{uid[:6].upper()}",
                    account_role="owner",
                    org_id=uid,
                )
                s.add(row)
                action = "created"
            else:
                row.name = name or row.name
                row.password_hash = auth.hash_password(password)
                row.password_algo = "argon2id"
                row.legacy_salt = None
                row.email_verified = True
                row.trial_until = max(float(row.trial_until or 0), trial_until)
                row.account_role = "owner"
                if not getattr(row, "org_id", None):
                    row.org_id = row.id
                # Keep firms separate: each owner is their own org.
                if row.account_role == "owner" and row.org_id != row.id:
                    row.org_id = row.id
                sub = (row.subscription_json or "").strip()
                if not sub or sub == "{}":
                    row.subscription_json = '{"status":"trialing","plan":"pro","seats":1}'
                s.add(row)
                action = "updated"
            results.append((email, password, name, action))
    return results


def _http_json(
    method: str,
    url: str,
    body: dict | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict | list | str]:
    data = None
    req_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.getcode() or 200
            try:
                return status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed: dict | list | str = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = raw
        return int(exc.code), parsed


def _seed_remote(base_url: str, accounts: list[dict[str, str]]) -> list[tuple[str, str, str, str]]:
    base = base_url.rstrip("/")
    admin = (os.environ.get("CONTROL_ADMIN_TOKEN") or "").strip()
    if not admin:
        raise SystemExit(
            "Remote seed requires CONTROL_ADMIN_TOKEN (for POST /v1/trial after signup)."
        )
    results: list[tuple[str, str, str, str]] = []
    for acct in accounts:
        email = acct["email"].strip().lower()
        name = acct["name"].strip()
        password = acct["password"]
        status, payload = _http_json(
            "POST",
            f"{base}/v1/signup",
            {
                "email": email,
                "password": password,
                "name": name,
                "accepted_terms": True,
            },
        )
        if status == 200:
            action = "created"
        elif status == 409:
            # Account exists — login proves password; trial grant still refreshes entitlement.
            login_status, _ = _http_json(
                "POST",
                f"{base}/v1/login",
                {"email": email, "password": password},
            )
            if login_status != 200:
                raise SystemExit(
                    f"Account {email} exists but password does not match. "
                    "Reset via local/DB seed with DATABASE_URL, or delete the user first."
                )
            action = "exists"
        else:
            raise SystemExit(f"Signup failed for {email}: HTTP {status} {payload!r}")

        trial_status, trial_payload = _http_json(
            "POST",
            f"{base}/v1/trial",
            {"email": email, "cohort": "demo"},
            headers={"X-Admin-Token": admin},
        )
        if trial_status != 200:
            # Domain already claimed (common when seeding multiple @casesifter.dev
            # demos) — login success above is enough; entitlement may already be trialing.
            detail = ""
            if isinstance(trial_payload, dict):
                detail = str(trial_payload.get("detail") or "")
            if trial_status == 403 and "already started" in detail.lower():
                action = f"{action}+trial-claim-exists"
            else:
                raise SystemExit(
                    f"Trial grant failed for {email}: HTTP {trial_status} {trial_payload!r}"
                )
        results.append((email, password, name, action))
    return results


def _print_table(results: list[tuple[str, str, str, str]], host: str) -> None:
    print(f"Demo firm accounts seeded ({host}).")
    print(f"{'email':<28} {'password':<28} {'firm':<14} action")
    print("-" * 90)
    for email, password, name, action in results:
        print(f"{email:<28} {password:<28} {name:<14} {action}")
    print(f"verified: true  trial_days: {TRIAL_DAYS}  role: owner (separate orgs)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed CaseSifter demo firm owners")
    parser.add_argument(
        "--remote",
        metavar="CONTROL_URL",
        help="Seed via HTTP against a live control plane (signup + admin trial)",
    )
    parser.add_argument(
        "--email",
        action="append",
        help="Optional: seed only this email (repeatable). Default: all four demos.",
    )
    args = parser.parse_args(argv)

    accounts = DEMO_ACCOUNTS
    if args.email:
        wanted = {e.strip().lower() for e in args.email}
        accounts = [a for a in DEMO_ACCOUNTS if a["email"] in wanted]
        if not accounts:
            print("No matching demo accounts for --email filters.", file=sys.stderr)
            return 2

    # Legacy single-account env overrides (keep one-off seeding working).
    if os.environ.get("CASESIFTER_DEMO_EMAIL") and not args.email and not args.remote:
        accounts = [
            {
                "email": (os.environ.get("CASESIFTER_DEMO_EMAIL") or "").strip().lower(),
                "name": (os.environ.get("CASESIFTER_DEMO_NAME") or "Demo Firm").strip(),
                "password": DEMO_PASSWORD,
            }
        ]

    if args.remote:
        results = _seed_remote(args.remote, accounts)
        _print_table(results, args.remote)
        print("Sign in at the app pointed at this control plane.")
        return 0

    guarded = _guard_production()
    if guarded is not None:
        return guarded

    results = _upsert_local(accounts)
    host = "DATABASE_URL" if (os.environ.get("DATABASE_URL") or "").strip() else "local auth store"
    _print_table(results, host)
    print("Sign in via Account on the app (local Vite or hosted app URL).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
