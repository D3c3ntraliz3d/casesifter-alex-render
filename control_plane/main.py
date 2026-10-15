"""CaseSifter control plane — Stripe billing + entitlement (no document vault)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

_DATA_ENV = (os.environ.get("CONTROL_DATA_DIR") or "").strip()
DATA = Path(_DATA_ENV) if _DATA_ENV else (Path(__file__).resolve().parent / "data")
DATA.mkdir(parents=True, exist_ok=True)
STORE = DATA / "accounts.json"

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
APP_PUBLIC_URL = os.environ.get("CASESIFTER_PUBLIC_URL", "http://127.0.0.1:5173")
TRIAL_DAYS = int(os.environ.get("CASESIFTER_TRIAL_DAYS", "14"))
INVITE_REQUIRED = os.environ.get("CASESIFTER_INVITE_REQUIRED", "false").lower() in ("1", "true", "yes")
INVITE_CODES = {
    part.strip().lower()
    for part in os.environ.get("CASESIFTER_INVITE_CODES", "").split(",")
    if part.strip()
}
SESSION_TTL_S = int(float(os.environ.get("SESSION_TTL_DAYS", "30") or 30) * 86400)
_SESSION_SECRET_PATH = DATA / "session_secret"

# Soft-launch: public signup off; only this firm account may sign in.
SINGLE_USER_EMAIL = (
    os.environ.get("CASESIFTER_SINGLE_USER_EMAIL") or "admin@libertygroupfunding.com"
).strip().lower()
SINGLE_USER_PASSWORD = os.environ.get("CASESIFTER_SINGLE_USER_PASSWORD") or "Casesifter1"
SINGLE_USER_NAME = (os.environ.get("CASESIFTER_SINGLE_USER_NAME") or "Liberty Group Funding").strip()


def _is_production() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def _resolve_session_secret() -> str:
    """Production: env SESSION_SECRET only (shared Blueprint group). Refuse if empty.

    Non-production: env, then disk pin, then generate for local tests.
    """
    import logging

    log = logging.getLogger("uvicorn.error")
    env = (os.environ.get("SESSION_SECRET") or "").strip()
    disk = ""
    if _SESSION_SECRET_PATH.is_file():
        try:
            disk = _SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            disk = ""

    if _is_production():
        if not env:
            raise RuntimeError(
                "SESSION_SECRET is required when APP_ENV=production "
                "(from envVarGroup casesifter-auth). Refusing to boot."
            )
        if disk and disk != env:
            log.warning(
                "Ignoring disk session_secret pin; using SESSION_SECRET from environment"
            )
        return env

    secret = env or disk or secrets.token_urlsafe(48)
    if secret and secret != disk:
        try:
            _SESSION_SECRET_PATH.write_text(secret, encoding="utf-8")
            try:
                os.chmod(_SESSION_SECRET_PATH, 0o600)
            except OSError:
                pass
        except OSError:
            pass
    return secret


# Resolved at import for handlers; production raises if SESSION_SECRET is empty.
SESSION_SECRET = _resolve_session_secret()


def _allow_dev_auth_tokens() -> bool:
    """Dev verify/reset tokens in HTTP responses — opt-in only (default off)."""
    return (os.environ.get("AUTH_DEV_TOKENS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

# Free consumer mail — trial limit is per email address, not per domain.
FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "ymail.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "gmx.com",
    "gmx.net",
    "mail.com",
    "zoho.com",
    "hey.com",
}

DOMAIN_TRIAL_CONFLICT = (
    "A trial was already started for this company email domain. "
    "Sign in to that account or contact support."
)
EMAIL_TRIAL_CONFLICT = (
    "A trial was already started for this email address. "
    "Sign in to that account or contact support."
)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=200)


class SignupBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str | None = None
    accepted_terms: bool = False
    invite_code: str | None = None


class TrialBody(BaseModel):
    email: EmailStr
    admin_token: str
    cohort: str | None = "beta"


class DiagnosticsBody(BaseModel):
    app_version: str | None = None
    platform: str | None = None
    entitlement_mode: str | None = None
    plan: str | None = None
    note: str | None = Field(default=None, max_length=2000)
    recent_events: list[dict[str, Any]] | None = None
    last_failure: dict[str, Any] | None = None


class CheckoutBody(BaseModel):
    trial: bool = False
    quantity: int = Field(default=1, ge=1, le=50)


def _ensure_auth_db() -> None:
    import auth_store as auth

    auth.init_auth_store()
    auth.migrate_accounts_json(STORE)


def _ensure_single_user() -> None:
    """Create or refresh the sole allowlisted account (verified + scans unlocked)."""
    import auth_store as auth
    from sqlalchemy import select

    if not SINGLE_USER_EMAIL or not SINGLE_USER_PASSWORD:
        return
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == SINGLE_USER_EMAIL)
        ).scalar_one_or_none()
        sub = json.dumps({"status": "active", "plan": "enterprise"})
        if row is None:
            row = auth.CpUser(
                id=uuid.uuid4().hex,
                email=SINGLE_USER_EMAIL,
                name=SINGLE_USER_NAME,
                password_hash=auth.hash_password(SINGLE_USER_PASSWORD),
                password_algo="argon2id",
                email_verified=True,
                created_at=time.time(),
                accepted_terms_at=time.time(),
                trial_until=0,
                subscription_json=sub,
                cohort="enterprise",
                support_code=_support_code(),
            )
            s.add(row)
            return
        # Keep password + entitlement in sync with the allowlist config.
        if not auth.verify_password(row, SINGLE_USER_PASSWORD):
            row.password_hash = auth.hash_password(SINGLE_USER_PASSWORD)
            row.password_algo = "argon2id"
            row.legacy_salt = None
        row.email_verified = True
        row.name = row.name or SINGLE_USER_NAME
        row.subscription_json = sub
        if not row.support_code:
            row.support_code = _support_code()
        s.add(row)


def _load() -> dict[str, Any]:
    """Compatibility facade over SQL auth store (atomic transactions)."""
    import auth_store as auth
    from sqlalchemy import select

    _ensure_auth_db()
    with auth.session_scope() as s:
        users: dict[str, Any] = {}
        for row in s.execute(select(auth.CpUser)).scalars().all():
            users[row.email] = auth.user_to_dict(row)
            # Keep password fields out of the facade dict used by Stripe paths.
        return {
            "users": users,
            "sessions": {},
            "trials": {},
            "trial_domains": auth.get_meta(s, "trial_domains"),
            "trial_emails": auth.get_meta(s, "trial_emails"),
        }


def _save(data: dict[str, Any]) -> None:
    """Persist user entitlement fields + trial claim maps (atomic)."""
    import auth_store as auth
    from sqlalchemy import select

    _ensure_auth_db()
    with auth.session_scope() as s:
        for email, u in (data.get("users") or {}).items():
            email_n = str(email).lower().strip()
            row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email_n)).scalar_one_or_none()
            if row is None:
                continue
            auth.apply_user_dict(row, u)
            if u.get("user_id"):
                # identity already on row.id
                pass
            if u.get("support_code"):
                row.support_code = u.get("support_code")
            s.add(row)
        auth.set_meta(s, "trial_domains", data.get("trial_domains") or {})
        auth.set_meta(s, "trial_emails", data.get("trial_emails") or {})


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _issue_session_token(email: str, data: dict[str, Any]) -> str:
    """HMAC-signed token when SESSION_SECRET is set; else opaque store-backed token."""
    email_l = email.lower().strip()
    if SESSION_SECRET:
        issued = int(time.time())
        nonce = secrets.token_hex(8)
        payload = f"{email_l}|{issued}|{nonce}".encode("utf-8")
        sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        return f"cs1.{_b64url_encode(payload)}.{_b64url_encode(sig)}"
    token = secrets.token_urlsafe(32)
    data["sessions"][token] = {"email": email_l, "created_at": time.time()}
    return token


def _verify_signed_session(token: str, data: dict[str, Any]) -> dict[str, Any] | None:
    if not SESSION_SECRET or not token.startswith("cs1."):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = _b64url_decode(parts[1])
        sig = _b64url_decode(parts[2])
    except Exception:
        return None
    expect = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        email, issued_s, _nonce = payload.decode("utf-8").split("|", 2)
        issued = int(issued_s)
    except Exception:
        return None
    if SESSION_TTL_S > 0 and (time.time() - issued) > SESSION_TTL_S:
        return None
    return data["users"].get(email.lower().strip())


def _email_domain(email: str) -> str:
    parts = str(email).lower().rsplit("@", 1)
    return parts[-1] if len(parts) == 2 else ""


def _is_free_email_provider(domain: str) -> bool:
    return domain.lower() in FREE_EMAIL_DOMAINS


def _trial_days_left(end_ts: float | None, now: float | None = None) -> int | None:
    if end_ts is None:
        return None
    try:
        end = float(end_ts)
    except (TypeError, ValueError):
        return None
    if end <= 0:
        return None
    now = time.time() if now is None else now
    if end <= now:
        return 0
    return int(math.ceil((end - now) / 86400))


def _claim_record(user: dict[str, Any]) -> dict[str, Any]:
    _ensure_user_identity(user)
    return {
        "email": str(user.get("email") or "").lower(),
        "started_at": time.time(),
        "user_id": user.get("user_id"),
    }


def _other_user_holds_trial_claim(data: dict[str, Any], user: dict[str, Any]) -> str | None:
    """Return a clear 403 message if another account already claimed this trial slot."""
    email = str(user.get("email") or "").lower()
    domain = _email_domain(email)
    user_id = user.get("user_id")
    if not domain:
        return None

    if _is_free_email_provider(domain):
        claim = (data.get("trial_emails") or {}).get(email)
        if claim and claim.get("user_id") and claim.get("user_id") != user_id:
            return EMAIL_TRIAL_CONFLICT
        # Concurrent: another account with same email is impossible; also block if
        # a different free-mail user somehow shares the claim key.
        return None

    claim = (data.get("trial_domains") or {}).get(domain)
    if claim and claim.get("user_id") and claim.get("user_id") != user_id:
        return DOMAIN_TRIAL_CONFLICT

    # Reject a second concurrent trial while another account on the domain is
    # already trialing/active (covers race before webhook claim lands).
    for other in (data.get("users") or {}).values():
        if not isinstance(other, dict):
            continue
        other_email = str(other.get("email") or "").lower()
        if other_email == email:
            continue
        if _email_domain(other_email) != domain:
            continue
        if other.get("user_id") and other.get("user_id") == user_id:
            continue
        other_sub = other.get("subscription") or {}
        other_status = str(other_sub.get("status") or "")
        other_trial = float(other.get("trial_until") or 0)
        if other_status in {"trialing", "active"} or other_trial > time.time():
            return DOMAIN_TRIAL_CONFLICT
    return None


def _assert_trial_available(data: dict[str, Any], user: dict[str, Any]) -> None:
    msg = _other_user_holds_trial_claim(data, user)
    if msg:
        raise HTTPException(403, msg)


def _record_trial_claim(data: dict[str, Any], user: dict[str, Any]) -> None:
    """Claim domain/email after trial actually starts (not on abandoned Checkout)."""
    email = str(user.get("email") or "").lower()
    domain = _email_domain(email)
    if not domain:
        return
    record = _claim_record(user)
    if _is_free_email_provider(domain):
        emails = data.setdefault("trial_emails", {})
        existing = emails.get(email)
        if existing and existing.get("user_id") and existing.get("user_id") != user.get("user_id"):
            return  # do not overwrite another account's claim
        if not existing:
            emails[email] = record
        return
    domains = data.setdefault("trial_domains", {})
    existing = domains.get(domain)
    if existing and existing.get("user_id") and existing.get("user_id") != user.get("user_id"):
        return
    if not existing:
        domains[domain] = record


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Legacy helper retained for admin/dev paths; new accounts use argon2id via auth_store."""
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return salt, digest


def _issue_session_for_user(user_email: str) -> tuple[str, str]:
    """Create a revocable server session and return (token, session_id)."""
    import auth_store as auth
    from sqlalchemy import select

    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == user_email.lower().strip())
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Invalid session.")
        sess = auth.create_session(s, row, ttl_s=SESSION_TTL_S)
        sid = sess.id
        email_l = row.email
        user_id = row.id
    if not SESSION_SECRET:
        # Opaque token = session id when no signing secret (dev only).
        return sid, sid
    issued = int(time.time())
    payload = f"{user_id}|{email_l}|{sid}|{issued}".encode("utf-8")
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
    token = f"cs2.{_b64url_encode(payload)}.{_b64url_encode(sig)}"
    return token, sid


def _parse_session_token(token: str) -> dict[str, Any] | None:
    """Return {user_id,email,sid,issued} for cs2 tokens, or legacy cs1 email."""
    if not token:
        return None
    if token.startswith("cs2.") and SESSION_SECRET:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = _b64url_decode(parts[1])
            sig = _b64url_decode(parts[2])
        except Exception:
            return None
        expect = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            user_id, email, sid, issued_s = payload.decode("utf-8").split("|", 3)
            issued = int(issued_s)
        except Exception:
            return None
        if SESSION_TTL_S > 0 and (time.time() - issued) > SESSION_TTL_S:
            return None
        return {"user_id": user_id, "email": email.lower(), "sid": sid, "issued": issued}
    if token.startswith("cs1.") and SESSION_SECRET:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = _b64url_decode(parts[1])
            sig = _b64url_decode(parts[2])
        except Exception:
            return None
        expect = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            email, issued_s, _nonce = payload.decode("utf-8").split("|", 2)
            issued = int(issued_s)
        except Exception:
            return None
        # Legacy cs1: max 7-day age + optional absolute cutoff (CS1_ACCEPT_UNTIL).
        cs1_max = int(float(os.environ.get("CS1_MAX_AGE_DAYS", "7") or 7) * 86400)
        now = time.time()
        raw_cut = (os.environ.get("CS1_ACCEPT_UNTIL") or "").strip()
        if raw_cut:
            try:
                cut = float(raw_cut) if raw_cut.replace(".", "", 1).isdigit() else None
                if cut is None:
                    from datetime import datetime

                    cut = datetime.fromisoformat(raw_cut.replace("Z", "+00:00")).timestamp()
                if now >= cut:
                    return None
            except Exception:
                return None
        if cs1_max > 0 and (now - issued) > cs1_max:
            return None
        if SESSION_TTL_S > 0 and (now - issued) > SESSION_TTL_S:
            return None
        return {"user_id": None, "email": email.lower(), "sid": None, "issued": issued, "legacy": True}
    # Opaque session id (dev / no secret)
    return {"user_id": None, "email": None, "sid": token, "issued": 0, "opaque": True}


def _support_code() -> str:
    n = secrets.randbelow(10**9)
    return f"CS-{n // 1000000:03d}-{(n // 1000) % 1000:03d}-{n % 1000:03d}"


def _ensure_user_identity(user: dict[str, Any], *, cohort: str | None = None) -> None:
    if not user.get("user_id"):
        user["user_id"] = uuid.uuid4().hex[:16]
    if not user.get("support_code"):
        user["support_code"] = _support_code()
    if cohort:
        user["cohort"] = cohort
    elif not user.get("cohort"):
        user["cohort"] = "general"


def _entitlement_payload(user: dict[str, Any]) -> dict[str, Any]:
    _ensure_user_identity(user)
    sub = user.get("subscription") or {}
    seats = sub.get("seats")
    try:
        seats_n = int(seats) if seats is not None else None
    except (TypeError, ValueError):
        seats_n = None
    return {
        **_user_plan(user),
        "email": user["email"],
        "support_code": user.get("support_code"),
        "user_id": user.get("user_id"),
        "cohort": user.get("cohort"),
        "seats": seats_n,
        "has_billing_portal": bool(user.get("stripe_customer_id")),
        # Soft-launch: anonymized finding outcomes are always on (no opt-out UI).
        "share_anonymized_outcomes": True,
    }


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    try:
        return dict(obj)
    except Exception:
        return {}


def _subscription_quantity(sub: dict[str, Any]) -> int | None:
    items = sub.get("items") or {}
    data = items.get("data") if isinstance(items, dict) else None
    if isinstance(data, list) and data:
        first = _as_dict(data[0])
        qty = first.get("quantity")
        try:
            return max(1, int(qty))
        except (TypeError, ValueError):
            return None
    return None


def _apply_subscription_fields(
    user: dict[str, Any],
    *,
    status: str | None,
    subscription_id: str | None = None,
    customer_id: str | None = None,
    seats: int | None = None,
    current_period_end: Any = None,
    trial_end: Any = None,
    plan: str | None = "pro",
) -> None:
    if customer_id:
        user["stripe_customer_id"] = str(customer_id)
    sub = dict(user.get("subscription") or {})
    if status is not None:
        sub["status"] = status
    if plan is not None:
        sub["plan"] = plan
    if subscription_id and str(subscription_id).startswith("sub_"):
        sub["stripe_subscription_id"] = str(subscription_id)
        # Legacy alias — never store Checkout session ids (cs_…).
        sub["stripe_id"] = str(subscription_id)
    if seats is not None:
        sub["seats"] = int(seats)
    if current_period_end is not None:
        sub["current_period_end"] = current_period_end
    if trial_end is not None:
        try:
            sub["trial_end"] = float(trial_end)
        except (TypeError, ValueError):
            pass
    user["subscription"] = sub


def _find_user_for_stripe(
    data: dict[str, Any], obj: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    meta = obj.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = _as_dict(meta)
    email = meta.get("email") or obj.get("customer_email") or obj.get("client_reference_id")
    if not email:
        # Invoice / some subscription payloads expose customer_details.
        details = obj.get("customer_details") or {}
        if isinstance(details, dict):
            email = details.get("email")
        else:
            email = _as_dict(details).get("email")
    if email:
        key = str(email).lower()
        user = data["users"].get(key)
        if user:
            return user, key
    customer_id = obj.get("customer")
    if customer_id:
        cid = str(customer_id)
        for key, user in data["users"].items():
            if user.get("stripe_customer_id") == cid:
                return user, key
    return None, None


def _invite_ok(code: str | None) -> bool:
    # When invite is not required, empty/missing codes are allowed.
    # If a code is supplied and a list is configured, still validate it.
    if not INVITE_REQUIRED:
        if not code or not str(code).strip():
            return True
        if not INVITE_CODES:
            return True
        return str(code).strip().lower() in INVITE_CODES
    if not code:
        return False
    return code.strip().lower() in INVITE_CODES


def _user_plan(user: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    trial_until = float(user.get("trial_until") or 0)
    sub = user.get("subscription") or {}
    status = str(sub.get("status") or "")
    trial_end_raw = sub.get("trial_end")
    try:
        stripe_trial_end = float(trial_end_raw) if trial_end_raw is not None else None
    except (TypeError, ValueError):
        stripe_trial_end = None
    period_end = sub.get("current_period_end")

    if status == "active":
        return {
            "active": True,
            "plan": sub.get("plan") or "pro",
            "message": "Subscription active.",
            "renews_at": period_end,
            "billing_state": "active",
            "trial_days_left": None,
        }
    if status == "trialing":
        end = stripe_trial_end or (trial_until if trial_until > now else None) or period_end
        days = _trial_days_left(end, now)
        return {
            "active": True,
            "plan": sub.get("plan") or "pro",
            "message": (
                f"Trial active — {days} day(s) left."
                if days is not None
                else "Trial active."
            ),
            "renews_at": period_end,
            "trial_until": end,
            "billing_state": "trialing",
            "trial_days_left": days,
        }
    if status == "past_due":
        return {
            "active": False,
            "plan": sub.get("plan") or "pro",
            "message": "Payment past due — update billing to restore scans.",
            "renews_at": period_end,
            "billing_state": "past_due",
            "trial_days_left": None,
        }
    if status == "canceled":
        # Local admin/dev trial can still unlock after cancel.
        if trial_until > now:
            days = _trial_days_left(trial_until, now)
            return {
                "active": True,
                "plan": "trial",
                "message": f"Trial active — {days} day(s) left.",
                "trial_until": trial_until,
                "billing_state": "trialing",
                "trial_days_left": days,
            }
        return {
            "active": False,
            "plan": None,
            "message": "Subscription canceled. Start a new trial or subscribe to analyze files.",
            "billing_state": "canceled",
            "trial_days_left": None,
        }
    if trial_until > now:
        days = _trial_days_left(trial_until, now)
        return {
            "active": True,
            "plan": "trial",
            "message": f"Trial active — {days} day(s) left.",
            "trial_until": trial_until,
            "billing_state": "trialing",
            "trial_days_left": days,
        }
    return {
        "active": False,
        "plan": None,
        "message": "Subscribe or start a trial to analyze files.",
        "billing_state": "none",
        "trial_days_left": None,
    }


_CORS_ORIGINS = [
    part.strip()
    for part in (os.environ.get("CONTROL_CORS_ORIGINS") or "").split(",")
    if part.strip()
] or [
    "https://app.casesifter.com",
    "https://www.casesifter.com",
    "https://casesifter.com",
    "https://casesifter-app.onrender.com",
    "https://casesifter-marketing.onrender.com",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
]

app = FastAPI(title="CaseSifter Control Plane", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory rate limits: key -> [timestamps]
_RATE: dict[str, list[float]] = {}
_RATE_LOCK = __import__("threading").Lock()


def _rate_limit(key: str, *, limit: int = 10, window_s: float = 600.0) -> None:
    now = time.time()
    with _RATE_LOCK:
        bucket = [t for t in _RATE.get(key, []) if now - t < window_s]
        if len(bucket) >= limit:
            raise HTTPException(429, "Too many attempts. Try again later.")
        bucket.append(now)
        _RATE[key] = bucket


@app.on_event("startup")
def _auth_startup() -> None:
    global SESSION_SECRET
    SESSION_SECRET = _resolve_session_secret()
    if _is_production() and not (os.environ.get("DATABASE_URL") or "").strip():
        raise RuntimeError(
            "DATABASE_URL is required when APP_ENV=production "
            "(fromDatabase casesifter-fact-memory). Refusing to boot."
        )
    _ensure_auth_db()
    _ensure_single_user()


@app.get("/health")
def health():
    # Checkout requires both secret + price id; expose that as stripe readiness.
    # RENDER_GIT_COMMIT is injected by Render at deploy — report the real live SHA.
    git_commit = (os.environ.get("RENDER_GIT_COMMIT") or "").strip() or None
    payload = {
        "status": "ok",
        "stripe": bool(STRIPE_SECRET and STRIPE_PRICE_ID),
        "git_commit_source": "RENDER_GIT_COMMIT",
        "git_commit": git_commit,
        "git_commit_short": git_commit[:12] if git_commit else None,
    }
    return payload


@app.post("/v1/signup")
def signup(body: SignupBody, request: Request):
    raise HTTPException(
        403,
        "Public signup is closed. Contact Sales@casesifter.com for firm access.",
    )


@app.post("/v1/login")
def login(body: LoginBody, request: Request):
    import auth_store as auth
    from sqlalchemy import select

    email = body.email.lower().strip()
    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"login:{client}:{email}", limit=20, window_s=600)
    if email != SINGLE_USER_EMAIL:
        raise HTTPException(401, "Invalid email or password.")
    _ensure_single_user()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None or not auth.verify_password(row, body.password):
            raise HTTPException(401, "Invalid email or password.")
        if not row.email_verified:
            raise HTTPException(403, "Verify your email before signing in.")
        auth.upgrade_password_if_needed(s, row, body.password)
        user_dict = auth.user_to_dict(row)
    token, _sid = _issue_session_for_user(email)
    return {"token": token, "entitlement": _entitlement_payload(user_dict)}


@app.post("/v1/logout")
def logout(authorization: str | None = Header(default=None)):
    import auth_store as auth

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required.")
    token = authorization[7:].strip()
    parsed = _parse_session_token(token)
    if not parsed or not parsed.get("sid"):
        # Still clear client-side; treat unknown as logged out.
        return {"ok": True}
    _ensure_auth_db()
    with auth.session_scope() as s:
        auth.revoke_session(s, parsed["sid"])
    return {"ok": True}


class VerifyEmailBody(BaseModel):
    token: str = Field(min_length=8, max_length=200)


@app.post("/v1/verify-email")
def verify_email(body: VerifyEmailBody):
    import auth_store as auth

    _ensure_auth_db()
    with auth.session_scope() as s:
        user = auth.consume_email_token(s, body.token, purpose="verify")
        if user is None:
            raise HTTPException(400, "Invalid or expired verification token.")
        user.email_verified = True
        s.add(user)
        email = user.email
        user_dict = auth.user_to_dict(user)
    token, _sid = _issue_session_for_user(email)
    return {"token": token, "entitlement": _entitlement_payload(user_dict)}


class ResetRequestBody(BaseModel):
    email: EmailStr


class ResetConfirmBody(BaseModel):
    token: str = Field(min_length=8, max_length=200)
    password: str = Field(min_length=8, max_length=200)


@app.post("/v1/password-reset/request")
def password_reset_request(body: ResetRequestBody, request: Request):
    import auth_store as auth
    from sqlalchemy import select

    email = str(body.email).lower().strip()
    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"reset:{client}:{email}", limit=5, window_s=3600)
    _ensure_auth_db()
    out: dict[str, Any] = {"ok": True, "message": "If that account exists, a reset link was sent."}
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is not None:
            raw = auth.create_email_token(s, row, purpose="reset", ttl_s=3600)
            if _allow_dev_auth_tokens():
                out["dev_reset_token"] = raw
    return out


@app.post("/v1/password-reset/confirm")
def password_reset_confirm(body: ResetConfirmBody):
    import auth_store as auth

    _ensure_auth_db()
    with auth.session_scope() as s:
        user = auth.consume_email_token(s, body.token, purpose="reset")
        if user is None:
            raise HTTPException(400, "Invalid or expired reset token.")
        user.password_hash = auth.hash_password(body.password)
        user.password_algo = "argon2id"
        user.legacy_salt = None
        s.add(user)
        # Revoke all sessions for this user.
        from sqlalchemy import select

        for sess in s.execute(select(auth.CpSession).where(auth.CpSession.user_id == user.id)).scalars():
            if sess.revoked_at is None:
                sess.revoked_at = time.time()
                s.add(sess)
    return {"ok": True}


class ExchangeBody(BaseModel):
    code: str = Field(min_length=8, max_length=200)


@app.post("/v1/session/exchange")
def exchange_session_code(body: ExchangeBody):
    """One-time 60s code → session token (replaces ?cs_token= URL handoff)."""
    import auth_store as auth

    _ensure_auth_db()
    with auth.session_scope() as s:
        sess = auth.consume_exchange_code(s, body.code)
        if sess is None:
            raise HTTPException(400, "Invalid or expired exchange code.")
        user = s.get(auth.CpUser, sess.user_id)
        if user is None:
            raise HTTPException(401, "Unknown user.")
        user_dict = auth.user_to_dict(user)
        sid = sess.id
        email = sess.email
        user_id = user.id
    if SESSION_SECRET:
        issued = int(time.time())
        payload = f"{user_id}|{email}|{sid}|{issued}".encode("utf-8")
        sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()
        token = f"cs2.{_b64url_encode(payload)}.{_b64url_encode(sig)}"
    else:
        token = sid
    return {"token": token, "entitlement": _entitlement_payload(user_dict)}


@app.post("/v1/session/handoff-code")
def create_handoff_code(authorization: str | None = Header(default=None)):
    """Mint a one-time exchange code for the current session (marketing→app)."""
    import auth_store as auth

    user, token, _data = _session_user(authorization)
    parsed = _parse_session_token(token)
    sid = (parsed or {}).get("sid")
    if not sid:
        raise HTTPException(400, "Session cannot mint a handoff code.")
    _ensure_auth_db()
    with auth.session_scope() as s:
        code = auth.create_exchange_code(s, sid, user["email"])
    return {"code": code, "expires_in": 60}


def _session_user(authorization: str | None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    import auth_store as auth
    from sqlalchemy import select

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required.")
    token = authorization[7:].strip()
    parsed = _parse_session_token(token)
    if not parsed:
        raise HTTPException(401, "Invalid session.")
    _ensure_auth_db()
    data = _load()
    with auth.session_scope() as s:
        sid = parsed.get("sid")
        if sid:
            sess = auth.get_active_session(s, sid)
            if sess is None:
                raise HTTPException(401, "Invalid session.")
            row = s.get(auth.CpUser, sess.user_id)
        elif parsed.get("email"):
            # Legacy cs1: require email still exists; no revoke sid.
            row = s.execute(
                select(auth.CpUser).where(auth.CpUser.email == parsed["email"])
            ).scalar_one_or_none()
        else:
            row = None
        if row is None:
            raise HTTPException(401, "Unknown user.")
        user = auth.user_to_dict(row)
    return user, token, data


@app.get("/v1/entitlement")
def entitlement(authorization: str | None = Header(default=None)):
    user, _, data = _session_user(authorization)
    if not user.get("support_code"):
        _ensure_user_identity(user)
        data["users"][user["email"]] = user
        _save(data)
    return _entitlement_payload(user)


@app.post("/v1/trial")
def grant_trial(body: TrialBody):
    """Sales/admin: mint or extend a trial. Requires CONTROL_ADMIN_TOKEN."""
    import auth_store as auth
    from sqlalchemy import select

    admin = os.environ.get("CONTROL_ADMIN_TOKEN", "")
    if not admin or body.admin_token != admin:
        raise HTTPException(403, "Admin token required.")
    email = body.email.lower().strip()
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            row = auth.CpUser(
                id=uuid.uuid4().hex,
                email=email,
                name="",
                password_hash=auth.hash_password(secrets.token_urlsafe(16)),
                password_algo="argon2id",
                email_verified=True,
                created_at=time.time(),
                trial_until=0,
                subscription_json="{}",
                cohort=body.cohort or "beta",
                support_code=_support_code(),
            )
            s.add(row)
        else:
            row.email_verified = True
            row.cohort = body.cohort or row.cohort or "beta"
            s.add(row)
        user = auth.user_to_dict(row)
    data = _load()
    _ensure_user_identity(user, cohort=body.cohort or "beta")
    _assert_trial_available(data, user)
    user["trial_until"] = time.time() + TRIAL_DAYS * 86400
    data["users"][email] = user
    _record_trial_claim(data, user)
    _save(data)
    return {
        "email": email,
        "trial_until": user["trial_until"],
        "entitlement": _entitlement_payload(user),
    }


@app.post("/v1/checkout")
def checkout(body: CheckoutBody = CheckoutBody(), authorization: str | None = Header(default=None)):
    user, _, data = _session_user(authorization)
    quantity = int(body.quantity)
    if body.trial:
        _ensure_user_identity(user)
        data["users"][user["email"]] = user
        _assert_trial_available(data, user)
    if not STRIPE_SECRET or not STRIPE_PRICE_ID:
        # Dev fallback: no Stripe configured, so there's no way to collect a card.
        u = data["users"][user["email"]]
        if body.trial:
            u["trial_until"] = time.time() + TRIAL_DAYS * 86400
            sub = dict(u.get("subscription") or {})
            sub["seats"] = quantity
            u["subscription"] = sub
            _record_trial_claim(data, u)
            message = "Stripe not configured — trial started without a card (local/dev)."
        else:
            _apply_subscription_fields(
                u,
                status="active",
                seats=quantity,
                current_period_end=time.time() + 30 * 86400,
                plan="pro-dev",
            )
            message = "Stripe not configured — marked Pro for local/dev."
        _save(data)
        return {
            "mode": "dev",
            "url": None,
            "message": message,
            "entitlement": _entitlement_payload(u),
        }
    try:
        import stripe

        stripe.api_key = STRIPE_SECRET
        meta = {"email": user["email"], "seats": str(quantity)}
        session_kwargs: dict[str, Any] = dict(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": quantity}],
            success_url=APP_PUBLIC_URL.rstrip("/") + "/?billing=success",
            cancel_url=APP_PUBLIC_URL.rstrip("/") + "/?billing=cancel",
            client_reference_id=user["email"],
            metadata=meta,
            # No tax collection yet — Managed Payments requires product tax_code.
            managed_payments={"enabled": False},
        )
        customer_id = user.get("stripe_customer_id")
        if customer_id:
            session_kwargs["customer"] = customer_id
        else:
            session_kwargs["customer_email"] = user["email"]
        subscription_data: dict[str, Any] = {"metadata": meta}
        if body.trial:
            # Card is still collected up front; billing simply doesn't start until the trial ends.
            subscription_data["trial_period_days"] = TRIAL_DAYS
        session_kwargs["subscription_data"] = subscription_data
        # Persist identity before Checkout so webhook claim can resolve user_id.
        _ensure_user_identity(user)
        data["users"][user["email"]] = user
        _save(data)
        session = stripe.checkout.Session.create(**session_kwargs)
        return {"mode": "stripe", "url": session.url}
    except Exception as exc:
        raise HTTPException(502, f"Stripe checkout failed: {exc}") from exc


@app.post("/v1/billing-portal")
def billing_portal(authorization: str | None = Header(default=None)):
    user, _, _ = _session_user(authorization)
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(
            400,
            "No Stripe customer yet — Subscribe or start a trial first.",
        )
    if not STRIPE_SECRET:
        raise HTTPException(503, "Stripe not configured.")
    try:
        import stripe

        stripe.api_key = STRIPE_SECRET
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=APP_PUBLIC_URL.rstrip("/"),
        )
        return {"url": session.url}
    except Exception as exc:
        raise HTTPException(502, f"Stripe billing portal failed: {exc}") from exc


@app.post("/v1/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    if not STRIPE_SECRET:
        return {"ignored": True}
    import stripe

    stripe.api_key = STRIPE_SECRET
    sig = request.headers.get("stripe-signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except Exception as exc:
        raise HTTPException(400, f"Webhook error: {exc}") from exc

    etype = event["type"] if isinstance(event, dict) else event.type
    raw_obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    obj = _as_dict(raw_obj)

    data = _load()
    user, email_key = _find_user_for_stripe(data, obj)

    if etype == "checkout.session.completed":
        if not user:
            return {"ok": True, "skipped": "unknown user"}
        customer_id = obj.get("customer")
        subscription_id = obj.get("subscription")
        meta = obj.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = _as_dict(meta)
        seats = None
        try:
            seats = int(meta.get("seats")) if meta.get("seats") is not None else None
        except (TypeError, ValueError):
            seats = None
        status = "active"
        period_end = None
        trial_end = None
        if subscription_id:
            try:
                sub = _as_dict(stripe.Subscription.retrieve(str(subscription_id)))
                status = sub.get("status") or status
                period_end = sub.get("current_period_end")
                trial_end = sub.get("trial_end")
                qty = _subscription_quantity(sub)
                if qty is not None:
                    seats = qty
                if not customer_id:
                    customer_id = sub.get("customer")
            except Exception:
                pass
        if seats is None:
            seats = 1
        # Never persist Checkout session id (cs_…) as the subscription id.
        sub_id = str(subscription_id) if subscription_id and str(subscription_id).startswith("sub_") else None
        _apply_subscription_fields(
            user,
            status=status,
            subscription_id=sub_id,
            customer_id=str(customer_id) if customer_id else None,
            seats=seats,
            current_period_end=period_end,
            trial_end=trial_end,
            plan="pro",
        )
        if status == "trialing" or trial_end:
            _record_trial_claim(data, user)
        data["users"][email_key or user["email"]] = user
        _save(data)
        return {"ok": True}

    if etype in {"customer.subscription.created", "customer.subscription.updated"}:
        if not user:
            # Resolve via subscription metadata.email if customer lookup missed.
            meta = obj.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = _as_dict(meta)
            if meta.get("email"):
                email_key = str(meta["email"]).lower()
                user = data["users"].get(email_key)
            if not user:
                return {"ok": True, "skipped": "unknown user"}
        seats = _subscription_quantity(obj)
        meta = obj.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = _as_dict(meta)
        if seats is None and meta.get("seats") is not None:
            try:
                seats = int(meta.get("seats"))
            except (TypeError, ValueError):
                seats = None
        sub_id = obj.get("id")
        if sub_id and not str(sub_id).startswith("sub_"):
            sub_id = None
        status = obj.get("status") or "active"
        _apply_subscription_fields(
            user,
            status=status,
            subscription_id=str(sub_id) if sub_id else None,
            customer_id=str(obj["customer"]) if obj.get("customer") else None,
            seats=seats,
            current_period_end=obj.get("current_period_end"),
            trial_end=obj.get("trial_end"),
            plan="pro",
        )
        if status == "trialing" or obj.get("trial_end"):
            _record_trial_claim(data, user)
        data["users"][email_key or user["email"]] = user
        _save(data)
        return {"ok": True}

    if etype == "customer.subscription.deleted":
        if not user:
            return {"ok": True, "skipped": "unknown user"}
        if obj.get("customer"):
            user["stripe_customer_id"] = str(obj["customer"])
        user["subscription"] = {
            "status": "canceled",
            "plan": None,
            "seats": (user.get("subscription") or {}).get("seats"),
            "stripe_subscription_id": (
                str(obj["id"]) if obj.get("id") and str(obj["id"]).startswith("sub_") else None
            ),
        }
        data["users"][email_key or user["email"]] = user
        _save(data)
        return {"ok": True}

    if etype == "invoice.payment_failed":
        if not user:
            # Try subscription metadata when invoice has no customer match yet.
            sub_ref = obj.get("subscription")
            if sub_ref:
                try:
                    sub = _as_dict(stripe.Subscription.retrieve(str(sub_ref)))
                    user, email_key = _find_user_for_stripe(data, sub)
                    if not user:
                        meta = sub.get("metadata") or {}
                        if not isinstance(meta, dict):
                            meta = _as_dict(meta)
                        if meta.get("email"):
                            email_key = str(meta["email"]).lower()
                            user = data["users"].get(email_key)
                except Exception:
                    pass
        if not user:
            return {"ok": True, "skipped": "unknown user"}
        _apply_subscription_fields(
            user,
            status="past_due",
            subscription_id=(
                str(obj["subscription"])
                if obj.get("subscription") and str(obj["subscription"]).startswith("sub_")
                else None
            ),
            customer_id=str(obj["customer"]) if obj.get("customer") else None,
            plan="pro",
        )
        data["users"][email_key or user["email"]] = user
        _save(data)
        return {"ok": True}

    return {"ok": True, "ignored": etype}


@app.post("/v1/dms/webhook")
async def dms_webhook_stub(request: Request):
    """Phase-2 stub: accept DMS notifications without storing documents."""
    body = await request.json()
    event_id = uuid.uuid4().hex
    log = DATA / "dms_events.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": event_id, "received_at": time.time(), "body": body}) + "\n")
    return {
        "accepted": True,
        "event_id": event_id,
        "message": (
            "DMS event recorded for the firm desktop agent to pull. "
            "CaseSifter control plane does not store the file bytes."
        ),
    }


@app.post("/v1/telemetry")
async def ingest_telemetry(request: Request, authorization: str | None = Header(default=None)):
    """Accept anonymized finding feedback / scan outcomes — never document bytes."""
    user_id = None
    support_code = None
    cohort = None
    if authorization:
        try:
            user, _, _ = _session_user(authorization)
            _ensure_user_identity(user)
            user_id = user.get("user_id")
            support_code = user.get("support_code")
            cohort = user.get("cohort")
        except HTTPException:
            pass
    body = await request.json()
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list):
        raise HTTPException(400, "events array required")
    if isinstance(body, dict):
        support_code = body.get("support_code") or support_code
        user_id = body.get("user_id") or user_id
    log = DATA / "telemetry.jsonl"
    kept = 0
    with log.open("a", encoding="utf-8") as handle:
        for event in events[:100]:
            if not isinstance(event, dict):
                continue
            handle.write(
                json.dumps(
                    {
                        "id": event.get("id") or uuid.uuid4().hex,
                        "type": event.get("type") or "unknown",
                        "payload": event.get("payload") or {},
                        "created_at": event.get("created_at") or time.time(),
                        "received_at": time.time(),
                        "user_id": user_id,
                        "support_code": support_code,
                        "cohort": cohort,
                    }
                )
                + "\n"
            )
            kept += 1
    return {"accepted": kept}


@app.post("/v1/diagnostics")
async def ingest_diagnostics(
    body: DiagnosticsBody,
    authorization: str | None = Header(default=None),
):
    """Privacy-safe support bundle — no PDFs or document text."""
    user_id = None
    support_code = None
    cohort = None
    email = None
    if authorization:
        user, _, _ = _session_user(authorization)
        _ensure_user_identity(user)
        user_id = user.get("user_id")
        support_code = user.get("support_code")
        cohort = user.get("cohort")
        email = user.get("email")
    log = DATA / "diagnostics.jsonl"
    record = {
        "id": uuid.uuid4().hex,
        "received_at": time.time(),
        "user_id": user_id,
        "support_code": support_code,
        "cohort": cohort,
        "email": email,
        "app_version": body.app_version,
        "platform": body.platform,
        "entitlement_mode": body.entitlement_mode,
        "plan": body.plan,
        "note": (body.note or "")[:2000] or None,
        "recent_events": (body.recent_events or [])[:20],
        "last_failure": body.last_failure,
    }
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return {"accepted": True, "support_code": support_code}
