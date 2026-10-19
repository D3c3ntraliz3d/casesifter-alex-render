"""Upsert a local demo firm owner with known credentials.

Run from the control_plane directory (uses CONTROL_DATA_DIR / local SQLite):

    python seed_demo_account.py

Does not print other users' emails. Password is for local/demo only — not a
production secret. Set CASESIFTER_DEMO_PASSWORD to override the default.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

# Allow `python seed_demo_account.py` from control_plane/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Use a deliverable-looking domain (.local is rejected by EmailStr / email-validator).
DEMO_EMAIL = (os.environ.get("CASESIFTER_DEMO_EMAIL") or "demo@casesifter.dev").strip().lower()
DEMO_PASSWORD = (
    os.environ.get("CASESIFTER_DEMO_PASSWORD") or "CaseSifter-Demo-2026!"
).strip()
DEMO_NAME = (os.environ.get("CASESIFTER_DEMO_NAME") or "Demo Firm").strip()
TRIAL_DAYS = int(os.environ.get("CASESIFTER_TRIAL_DAYS", "14") or 14)


def main() -> int:
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        # Refuse to plant a known password into a production auth DB by accident.
        if (os.environ.get("CASESIFTER_ALLOW_DEMO_SEED") or "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            print(
                "Refusing to seed demo account while APP_ENV=production "
                "(set CASESIFTER_ALLOW_DEMO_SEED=true to override).",
                file=sys.stderr,
            )
            return 2

    import auth_store as auth
    from sqlalchemy import select

    auth.init_auth_store()
    trial_until = time.time() + TRIAL_DAYS * 86400
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == DEMO_EMAIL)).scalar_one_or_none()
        if row is None:
            uid = uuid.uuid4().hex
            row = auth.CpUser(
                id=uid,
                email=DEMO_EMAIL,
                name=DEMO_NAME,
                password_hash=auth.hash_password(DEMO_PASSWORD),
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
            row.name = DEMO_NAME or row.name
            row.password_hash = auth.hash_password(DEMO_PASSWORD)
            row.password_algo = "argon2id"
            row.legacy_salt = None
            row.email_verified = True
            row.trial_until = max(float(row.trial_until or 0), trial_until)
            row.account_role = "owner"
            if not getattr(row, "org_id", None):
                row.org_id = row.id
            sub = (row.subscription_json or "").strip()
            if not sub or sub == "{}":
                row.subscription_json = '{"status":"trialing","plan":"pro","seats":1}'
            s.add(row)
            action = "updated"

    print(f"Demo firm account {action} (local auth store).")
    print(f"  email:    {DEMO_EMAIL}")
    print(f"  password: {DEMO_PASSWORD}")
    print(f"  verified: true  trial_days: {TRIAL_DAYS}  role: owner")
    print("Sign in via Account on the app (local Vite or hosted app URL).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
