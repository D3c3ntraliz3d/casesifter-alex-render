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

from fastapi import Cookie, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field

import mailer
import rate_limit
from edge_security import docs_kwargs, install_api_headers
from session_cookie import COOKIE_NAME, clear_session_cookie, set_session_cookie

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


def _is_production() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def _require_email_verify() -> bool:
    """When false (default), signup marks verified and login skips the verify gate.

    Set CASESIFTER_REQUIRE_EMAIL_VERIFY=true to enforce verify-before-login.
    /v1/verify-email stays available either way.
    """
    return (os.environ.get("CASESIFTER_REQUIRE_EMAIL_VERIFY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


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
    password: str = Field(min_length=8, max_length=200)


class SignupBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    name: str | None = None
    accepted_terms: bool = False
    invite_code: str | None = None
    # Seat invite token from Admin → Invite member (joins org as member/admin).
    seat_invite_token: str | None = None


class TrialBody(BaseModel):
    email: EmailStr
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


def _signature_ok(payload: bytes, sig: bytes) -> bool:
    """Accept the current secret and, during rotation, SESSION_SECRET_PREVIOUS."""
    secrets: list[str] = []
    for secret in (SESSION_SECRET, (os.environ.get("SESSION_SECRET_PREVIOUS") or "").strip()):
        if secret and secret not in secrets:
            secrets.append(secret)
    for secret in secrets:
        expect = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if hmac.compare_digest(sig, expect):
            return True
    return False


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
    if not _signature_ok(payload, sig):
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
        if not _signature_ok(payload, sig):
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
        if not _signature_ok(payload, sig):
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
    # Billing fields may already be merged from org owner in _session_user.
    billing = user.get("_billing") or user
    sub = billing.get("subscription") or user.get("subscription") or {}
    seats = sub.get("seats")
    try:
        seats_n = int(seats) if seats is not None else None
    except (TypeError, ValueError):
        seats_n = None
    import oidc_auth

    plan_user = {**user, "subscription": sub, "trial_until": billing.get("trial_until", user.get("trial_until"))}
    return {
        **_user_plan(plan_user),
        "email": user["email"],
        "support_code": user.get("support_code"),
        "user_id": user.get("user_id"),
        "org_id": user.get("org_id") or user.get("user_id"),
        "org_owner_email": user.get("org_owner_email"),
        "cohort": user.get("cohort"),
        "seats": seats_n,
        "seats_used": user.get("seats_used"),
        "has_billing_portal": bool(
            billing.get("stripe_customer_id") or user.get("stripe_customer_id")
        ),
        # Soft-launch: anonymized finding outcomes are always on (no opt-out UI).
        "share_anonymized_outcomes": True,
        "totp_enabled": bool(user.get("totp_enabled")),
        "oidc_linked": bool(user.get("oidc_linked")),
        "account_role": user.get("account_role") or "owner",
        "is_account_admin": (user.get("account_role") or "owner") in {"owner", "admin"},
        "sso_available": oidc_auth.oidc_configured(),
        "sso_preferred": bool(user.get("sso_preferred")),
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

app = FastAPI(title="CaseSifter Control Plane", version="1.0.0", **docs_kwargs(os.environ.get("APP_ENV")))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_api_headers(app)

_ORIGIN_ALLOW = {origin.rstrip("/") for origin in _CORS_ORIGINS}


@app.middleware("http")
async def _cookie_csrf(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get(COOKIE_NAME):
        authorization = request.headers.get("authorization") or ""
        if not authorization.lower().startswith("bearer "):
            origin = (request.headers.get("origin") or "").rstrip("/")
            if _is_production():
                allowed = origin in _ORIGIN_ALLOW
            else:
                allowed = not origin or origin in _ORIGIN_ALLOW
            if not allowed:
                return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
    return await call_next(request)


def _rate_limit(key: str, *, limit: int = 10, window_s: float = 600.0) -> None:
    rate_limit.enforce(key, limit=limit, window_s=window_s)


def _request_id(request: Request) -> str:
    incoming = (request.headers.get("x-request-id") or "").strip()
    return incoming[:64] if incoming else uuid.uuid4().hex


def _client_is_desktop(request: Request) -> bool:
    return (request.headers.get("x-casesifter-client") or "").strip().lower() == "desktop"


def _accept_bearer_in_body(request: Request) -> bool:
    """Embedded browsers that block cross-site cookies may request the opaque session token in JSON."""
    flag = (request.headers.get("x-casesifter-accept-bearer") or "").strip().lower()
    return flag in {"1", "true", "yes"}


def _include_session_token(request: Request) -> bool:
    """Desktop, local tests, and cookie-blocked browsers (Accept-Bearer) receive the token."""
    if _client_is_desktop(request):
        return True
    if _accept_bearer_in_body(request):
        return True
    return not _is_production()


def _internal_exchange(request: Request) -> bool:
    """Analysis API proves it holds SESSION_SECRET. Browsers cannot."""
    presented = (request.headers.get("x-internal-session") or "").strip()
    secret = (SESSION_SECRET or "").strip()
    if not presented or not secret:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(),
        hashlib.sha256(secret.encode("utf-8")).digest(),
    )


def _admin_token_ok(presented: str | None) -> bool:
    expected = (os.environ.get("CONTROL_ADMIN_TOKEN") or "").strip()
    got = (presented or "").strip()
    if not expected or not got:
        return False
    return hmac.compare_digest(
        hashlib.sha256(got.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


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


@app.get("/health")
def health():
    # Checkout requires both secret + price id; expose that as stripe readiness.
    # RENDER_GIT_COMMIT is injected by Render at deploy — report the real live SHA.
    git_commit = (os.environ.get("RENDER_GIT_COMMIT") or "").strip() or None
    payload = {
        "status": "ok",
        "stripe": bool(STRIPE_SECRET and STRIPE_PRICE_ID),
        "email": mailer.mailer_configured(),
        "git_commit_source": "RENDER_GIT_COMMIT",
        "git_commit": git_commit,
        "git_commit_short": git_commit[:12] if git_commit else None,
    }
    return payload


def _marketing_base() -> str:
    return (
        os.environ.get("CASESIFTER_MARKETING_URL") or "https://casesifter.com"
    ).rstrip("/")


def _record_auth(actor: str, action: str, request: Request, target: str | None = None) -> None:
    import auth_store as auth

    _ensure_auth_db()
    with auth.session_scope() as s:
        auth.record_auth_audit(
            s,
            actor=actor,
            action=action,
            target=target,
            request_id=_request_id(request),
        )


def _analysis_code_for(sid: str, email: str) -> str | None:
    if not sid:
        return None
    import auth_store as auth

    _ensure_auth_db()
    with auth.session_scope() as s:
        return auth.create_exchange_code(s, sid, email)


def _session_response(email: str, user_dict: dict[str, Any], request: Request) -> JSONResponse:
    import auth_store as auth

    token, sid = _issue_session_for_user(email)
    body: dict[str, Any] = {
        "entitlement": _entitlement_payload(user_dict),
        "analysis_code": _analysis_code_for(sid, email),
    }
    # One-time code for browsers that block cross-site Set-Cookie on the login
    # response (embedded/automated browsers). Frontend may retry via ?cs_code=.
    try:
        _ensure_auth_db()
        with auth.session_scope() as s:
            body["handoff_code"] = auth.create_exchange_code(s, sid, email)
    except Exception:
        pass
    if _include_session_token(request) or _internal_exchange(request):
        body["token"] = token
    response = JSONResponse(body)
    set_session_cookie(response, token, max_age=SESSION_TTL_S)
    return response


@app.post("/v1/signup")
def signup(body: SignupBody, request: Request):
    if not body.accepted_terms:
        raise HTTPException(422, "You must agree to the Terms of Service and Privacy Policy to continue.")
    # Seat invite bypasses the beta gate (org owner already entitled).
    seat_token = (body.seat_invite_token or "").strip() or None
    if not seat_token and not _invite_ok(body.invite_code):
        raise HTTPException(403, "A valid invite code is required for beta signup.")
    import auth_store as auth
    from sqlalchemy import select

    email = body.email.lower().strip()
    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"signup:{client}:{email}", limit=5, window_s=3600)
    require_verify = _require_email_verify()
    if (
        require_verify
        and _is_production()
        and not mailer.mailer_configured()
        and not _allow_dev_auth_tokens()
    ):
        raise HTTPException(503, "Email delivery is not configured.")
    _ensure_auth_db()
    with auth.session_scope() as s:
        existing = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "Account already exists.")
        seat_invite = None
        account_role = "owner"
        org_id = None
        if seat_token:
            seat_invite = auth.find_seat_invite_by_token(s, seat_token)
            if seat_invite is None:
                raise HTTPException(400, "Seat invite is invalid or expired.")
            if seat_invite.email != email:
                raise HTTPException(400, "Sign up with the invited email address.")
            account_role = (seat_invite.role or "member").strip().lower()
            if account_role not in {"admin", "member"}:
                account_role = "member"
            org_id = seat_invite.org_id
        cohort = "beta" if (body.invite_code or INVITE_REQUIRED or seat_invite) else "general"
        uid = uuid.uuid4().hex
        verified_now = not require_verify
        user = auth.CpUser(
            id=uid,
            email=email,
            name=body.name or "",
            password_hash=auth.hash_password(body.password),
            password_algo="argon2id",
            email_verified=verified_now,
            created_at=time.time(),
            accepted_terms_at=time.time(),
            trial_until=0,
            subscription_json="{}",
            cohort=cohort,
            support_code=_support_code(),
            account_role=account_role,
            org_id=org_id or uid,
        )
        s.add(user)
        if seat_invite is not None:
            seat_invite.accepted_at = time.time()
            s.add(seat_invite)
        verify_raw = auth.create_email_token(s, user, purpose="verify", ttl_s=86400)
        link = f"{_marketing_base()}/verify.html?verify_token={verify_raw}"
        mailed = False
        if mailer.mailer_configured() and require_verify:
            mailed = mailer.send_auth_email(
                email,
                "Verify your CaseSifter account",
                (
                    "Confirm your email to sign in to CaseSifter.\n\n"
                    f"{link}\n\n"
                    "This link expires in 24 hours. If you did not sign up, ignore this message."
                ),
            )
        if (
            require_verify
            and not mailed
            and _is_production()
            and not _allow_dev_auth_tokens()
        ):
            # Roll back the new account so a failed send does not leave an unverified user.
            raise HTTPException(503, "We could not send the verification email. Try again shortly.")
    payload = {
        "ok": True,
        "email": email,
        "email_verified": verified_now,
        "message": (
            "Account created. You can sign in now."
            if verified_now
            else "Check your email to verify your account before signing in."
        ),
    }
    # A sent mail must not also echo the token. Dev tokens only when mail was not sent.
    if require_verify and not mailed and _allow_dev_auth_tokens():
        payload["dev_verify_token"] = verify_raw
    return payload


@app.post("/v1/login")
def login(body: LoginBody, request: Request):
    import auth_store as auth
    from sqlalchemy import select

    email = body.email.lower().strip()
    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"login:{client}:{email}", limit=20, window_s=600)
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None or not auth.verify_password(row, body.password):
            _record_auth(email, "login.failure", request)
            raise HTTPException(401, "Invalid email or password.")
        if _require_email_verify() and not row.email_verified:
            raise HTTPException(403, "Verify your email before signing in.")
        auth.upgrade_password_if_needed(s, row, body.password)
        if bool(getattr(row, "totp_enabled", False)):
            mfa_token = auth.create_mfa_challenge(s, row, ttl_s=300)
            _record_auth(email, "login.mfa_required", request)
            return {
                "mfa_required": True,
                "mfa_token": mfa_token,
                "email": email,
                "message": "Enter the code from your authenticator app (or a recovery code).",
            }
        user_dict = auth.user_to_dict(row)
    _record_auth(email, "login.success", request)
    return _session_response(email, user_dict, request)


class MfaLoginBody(BaseModel):
    mfa_token: str = Field(min_length=8, max_length=200)
    code: str = Field(min_length=6, max_length=32)


@app.post("/v1/login/mfa")
def login_mfa(body: MfaLoginBody, request: Request):
    """Complete password login after TOTP or recovery code. No admin bypass."""
    import auth_store as auth
    import totp_auth

    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"login-mfa:{client}", limit=30, window_s=600)
    _ensure_auth_db()
    with auth.session_scope() as s:
        user = auth.consume_mfa_challenge(s, body.mfa_token)
        if user is None:
            raise HTTPException(401, "Invalid or expired MFA challenge.")
        email = user.email
        code = (body.code or "").strip()
        ok = False
        used_recovery = False
        if getattr(user, "totp_secret_enc", None):
            try:
                secret = totp_auth.decrypt_secret(user.totp_secret_enc)
                ok = totp_auth.verify_code(secret, code)
            except Exception:
                ok = False
        if not ok:
            hashes = totp_auth.load_recovery_hashes(getattr(user, "recovery_codes_json", None))
            remaining = totp_auth.consume_recovery_code(hashes, code)
            if remaining is not None:
                ok = True
                used_recovery = True
                user.recovery_codes_json = totp_auth.serialize_recovery_hashes(remaining)
                s.add(user)
        if not ok:
            _record_auth(email, "login.mfa_failure", request)
            raise HTTPException(401, "Invalid authenticator or recovery code.")
        user_dict = auth.user_to_dict(user)
    if used_recovery:
        _record_auth(email, "2fa.recovery_used", request)
    _record_auth(email, "login.success", request, target="mfa")
    return _session_response(email, user_dict, request)


@app.post("/v1/logout")
def logout(
    request: Request,
    authorization: str | None = Header(default=None),
    cs_session: str | None = Cookie(default=None),
):
    import auth_store as auth

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif cs_session:
        token = cs_session.strip()
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    if not token:
        return response
    parsed = _parse_session_token(token)
    if not parsed or not parsed.get("sid"):
        return response
    _ensure_auth_db()
    with auth.session_scope() as s:
        auth.revoke_session(s, parsed["sid"])
    return response


class VerifyEmailBody(BaseModel):
    token: str = Field(min_length=8, max_length=200)


@app.post("/v1/verify-email")
def verify_email(body: VerifyEmailBody, request: Request):
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
    _record_auth(email, "login.success", request, target="verify")
    return _session_response(email, user_dict, request)


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
            link = f"{_marketing_base()}/reset.html?reset_token={raw}"
            mailed = False
            if mailer.mailer_configured():
                mailed = mailer.send_auth_email(
                    email,
                    "Reset your CaseSifter password",
                    (
                        "Use this link to choose a new CaseSifter password.\n\n"
                        f"{link}\n\n"
                        "This link expires in one hour. If you did not ask for a reset, ignore this message."
                    ),
                )
            if not mailed and _allow_dev_auth_tokens():
                out["dev_reset_token"] = raw
    return out


@app.post("/v1/password-reset/confirm")
def password_reset_confirm(body: ResetConfirmBody, request: Request):
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
        email = user.email
        totp_on = bool(getattr(user, "totp_enabled", False))
        user_dict = auth.user_to_dict(user)
        # Revoke all sessions for this user.
        from sqlalchemy import select

        for sess in s.execute(select(auth.CpSession).where(auth.CpSession.user_id == user.id)).scalars():
            if sess.revoked_at is None:
                sess.revoked_at = time.time()
                s.add(sess)
        # 2FA must not be bypassed by reset: when TOTP is on, require a normal MFA login.
        if totp_on:
            mfa_token = auth.create_mfa_challenge(s, user, ttl_s=300)
            _record_auth(email, "login.mfa_required", request, target="password-reset")
            return {
                "ok": True,
                "mfa_required": True,
                "mfa_token": mfa_token,
                "email": email,
                "message": "Password updated. Enter your authenticator or recovery code to sign in.",
            }
    _record_auth(email, "login.success", request, target="password-reset")
    return _session_response(email, user_dict, request)


class ExchangeBody(BaseModel):
    code: str = Field(min_length=8, max_length=200)


@app.post("/v1/session/exchange")
def exchange_session_code(body: ExchangeBody, request: Request):
    """One-time 60s code → session cookie. Token JSON is for the analysis server only."""
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
    fresh_code = _analysis_code_for(sid, email)
    body_out: dict[str, Any] = {
        "entitlement": _entitlement_payload(user_dict),
        "analysis_code": fresh_code,
    }
    if _include_session_token(request) or _internal_exchange(request):
        body_out["token"] = token
    response = JSONResponse(body_out)
    set_session_cookie(response, token, max_age=SESSION_TTL_S)
    return response


@app.post("/v1/session/handoff-code")
def create_handoff_code(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Mint a one-time exchange code for the current session (marketing→app)."""
    import auth_store as auth

    user, token, _data = _session_user(authorization, request)
    parsed = _parse_session_token(token)
    sid = (parsed or {}).get("sid")
    if not sid:
        raise HTTPException(400, "Session cannot mint a handoff code.")
    _ensure_auth_db()
    with auth.session_scope() as s:
        code = auth.create_exchange_code(s, sid, user["email"])
    return {"code": code, "expires_in": 60}


def _session_user(
    authorization: str | None = None,
    request: Request | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    import auth_store as auth
    from sqlalchemy import select

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif request is not None:
        token = (request.cookies.get(COOKIE_NAME) or "").strip()
    if not token:
        raise HTTPException(401, "Bearer token required.")
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
        auth.ensure_org_id(s, row)
        user = auth.user_to_dict(row)
        owner = auth.billing_owner(s, row)
        if owner.id != row.id:
            owner_dict = auth.user_to_dict(owner)
            user["_billing"] = owner_dict
            user["subscription"] = owner_dict.get("subscription") or {}
            user["trial_until"] = owner_dict.get("trial_until")
            user["stripe_customer_id"] = owner_dict.get("stripe_customer_id")
            user["org_owner_email"] = owner_dict.get("email")
        else:
            user["org_owner_email"] = user.get("email")
        try:
            user["seats_used"] = auth.count_org_seats_used(s, auth.org_id_of(row))
        except Exception:
            user["seats_used"] = None
    return user, token, data


@app.get("/v1/audit")
def list_sign_in_audit(request: Request):
    """This account's sign-in events. Other firms and admin grants are not included."""
    user, _token, _data = _session_user(request=request)
    import auth_store as auth

    _ensure_auth_db()
    with auth.session_scope() as s:
        events = auth.list_sign_in_events(s, user.get("email") or "", limit=50)
    return {"events": events}


@app.get("/v1/entitlement")
def entitlement(request: Request, authorization: str | None = Header(default=None)):
    user, _, data = _session_user(authorization, request)
    if not user.get("support_code"):
        _ensure_user_identity(user)
        data["users"][user["email"]] = user
        _save(data)
    return _entitlement_payload(user)


@app.post("/v1/trial")
def grant_trial(
    body: TrialBody,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    """Sales/admin: mint or extend a trial. Requires CONTROL_ADMIN_TOKEN."""
    import auth_store as auth
    from sqlalchemy import select

    _rate_limit("trial:admin", limit=10, window_s=600)
    if not _admin_token_ok(x_admin_token):
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
    _record_auth("admin", "trial.grant", request, target=email)
    return {
        "email": email,
        "trial_until": user["trial_until"],
        "entitlement": _entitlement_payload(user),
    }


@app.post("/v1/checkout")
def checkout(
    request: Request,
    body: CheckoutBody = CheckoutBody(),
    authorization: str | None = Header(default=None),
):
    user, _, data = _session_user(authorization, request)
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
def billing_portal(request: Request, authorization: str | None = Header(default=None)):
    user, _, _ = _session_user(authorization, request)
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
            user, _, _ = _session_user(authorization, request)
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
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Privacy-safe support bundle — no PDFs or document text."""
    user_id = None
    support_code = None
    cohort = None
    email = None
    if authorization:
        user, _, _ = _session_user(authorization, request)
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


# ── Auth status / SSO discovery ──────────────────────────────────────────────


@app.get("/v1/auth/status")
def auth_status():
    """Public: whether SSO is configured (no secrets)."""
    import oidc_auth
    import totp_auth

    return {
        "sso_available": oidc_auth.oidc_configured(),
        "totp_available": totp_auth.totp_available(),
        "oidc_trusts_idp_mfa": oidc_auth.trusts_idp_mfa(),
    }


# ── TOTP enrollment (password users) ─────────────────────────────────────────


class TotpConfirmBody(BaseModel):
    code: str = Field(min_length=6, max_length=12)


class TotpDisableBody(BaseModel):
    password: str = Field(min_length=8, max_length=200)
    code: str = Field(min_length=6, max_length=32)


@app.post("/v1/2fa/totp/start")
def totp_start(request: Request, authorization: str | None = Header(default=None)):
    import auth_store as auth
    import totp_auth
    from sqlalchemy import select

    if not totp_auth.totp_available():
        raise HTTPException(503, "Authenticator setup is unavailable on this server.")
    user, _, _ = _session_user(authorization, request)
    email = user["email"]
    _rate_limit(f"2fa-start:{email}", limit=10, window_s=600)
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        if row.totp_enabled:
            raise HTTPException(409, "Two-factor authentication is already enabled.")
        secret = totp_auth.generate_secret()
        row.totp_secret_enc = totp_auth.encrypt_secret(secret)
        row.totp_enabled = False
        row.totp_confirmed_at = None
        s.add(row)
    _record_auth(email, "2fa.enroll_start", request)
    return {
        "secret": secret,
        "otpauth_url": totp_auth.provisioning_uri(secret, email),
        "issuer": totp_auth.ISSUER,
        "message": "Scan the otpauth URL with your authenticator app, then confirm with a 6-digit code.",
    }


@app.post("/v1/2fa/totp/confirm")
def totp_confirm(
    body: TotpConfirmBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    import auth_store as auth
    import totp_auth
    from sqlalchemy import select

    user, _, _ = _session_user(authorization, request)
    email = user["email"]
    _rate_limit(f"2fa-confirm:{email}", limit=20, window_s=600)
    _ensure_auth_db()
    recovery_plain: list[str] = []
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None or not row.totp_secret_enc:
            raise HTTPException(400, "Start authenticator enrollment first.")
        try:
            secret = totp_auth.decrypt_secret(row.totp_secret_enc)
        except Exception as exc:
            raise HTTPException(500, "Could not read authenticator secret.") from exc
        if not totp_auth.verify_code(secret, body.code):
            raise HTTPException(401, "Invalid authenticator code.")
        recovery_plain = totp_auth.mint_recovery_codes()
        row.recovery_codes_json = totp_auth.serialize_recovery_hashes(
            totp_auth.hash_recovery_codes(recovery_plain)
        )
        row.totp_enabled = True
        row.totp_confirmed_at = time.time()
        s.add(row)
    _record_auth(email, "2fa.enroll_confirm", request)
    return {
        "ok": True,
        "totp_enabled": True,
        "recovery_codes": recovery_plain,
        "message": "Store these recovery codes offline. Each can be used once.",
    }


@app.post("/v1/2fa/totp/disable")
def totp_disable(
    body: TotpDisableBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Disable 2FA. Requires password + current TOTP/recovery. Admins cannot skip this."""
    import auth_store as auth
    import totp_auth
    from sqlalchemy import select

    user, _, _ = _session_user(authorization, request)
    email = user["email"]
    _rate_limit(f"2fa-disable:{email}", limit=10, window_s=600)
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        if not auth.verify_password(row, body.password):
            raise HTTPException(401, "Invalid password.")
        ok = False
        if row.totp_secret_enc:
            try:
                ok = totp_auth.verify_code(totp_auth.decrypt_secret(row.totp_secret_enc), body.code)
            except Exception:
                ok = False
        if not ok:
            remaining = totp_auth.consume_recovery_code(
                totp_auth.load_recovery_hashes(row.recovery_codes_json), body.code
            )
            ok = remaining is not None
        if not ok:
            raise HTTPException(401, "Invalid authenticator or recovery code.")
        row.totp_secret_enc = None
        row.totp_enabled = False
        row.totp_confirmed_at = None
        row.recovery_codes_json = "[]"
        s.add(row)
    _record_auth(email, "2fa.disable", request)
    return {"ok": True, "totp_enabled": False}


# ── OIDC SSO ─────────────────────────────────────────────────────────────────


@app.get("/v1/sso/oidc/start")
def sso_oidc_start(request: Request):
    import auth_store as auth
    import oidc_auth

    if not oidc_auth.oidc_configured():
        raise HTTPException(503, "SSO is not configured.")
    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"sso-start:{client}", limit=30, window_s=600)
    nonce = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(48)
    _ensure_auth_db()
    with auth.session_scope() as s:
        state = auth.create_oidc_state(s, nonce=nonce, code_verifier=verifier, ttl_s=600)
    try:
        url, _ = oidc_auth.build_authorize_redirect(
            state=state, nonce=nonce, code_verifier=verifier
        )
    except Exception as exc:
        raise HTTPException(503, f"SSO discovery failed: {exc}") from exc
    return RedirectResponse(url, status_code=302)


@app.get("/v1/sso/oidc/callback")
def sso_oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    import auth_store as auth
    import oidc_auth
    from sqlalchemy import select

    if error:
        _record_auth("sso", "login.sso_failure", request, target=error[:120])
        raise HTTPException(400, f"SSO provider error: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing OIDC code or state.")
    client = (request.client.host if request.client else "unknown") or "unknown"
    _rate_limit(f"sso-callback:{client}", limit=40, window_s=600)
    if not oidc_auth.oidc_configured():
        raise HTTPException(503, "SSO is not configured.")
    _ensure_auth_db()
    with auth.session_scope() as s:
        st = auth.consume_oidc_state(s, state)
        if st is None:
            _record_auth("sso", "login.sso_failure", request, target="bad_state")
            raise HTTPException(400, "Invalid or expired SSO state.")
        nonce = st.nonce
        verifier = st.code_verifier
    try:
        tokens = oidc_auth.exchange_code(code=code, code_verifier=verifier)
        claims = oidc_auth.parse_id_token(tokens.get("id_token") or "", expected_nonce=nonce)
    except Exception as exc:
        _record_auth("sso", "login.sso_failure", request, target=str(exc)[:120])
        raise HTTPException(400, f"SSO login failed: {exc}") from exc

    email = claims["email"]
    issuer = claims["iss"]
    subject = claims["sub"]
    name = claims.get("name") or ""

    with auth.session_scope() as s:
        row = auth.find_user_by_oidc(s, issuer=issuer, subject=subject)
        if row is None:
            row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            # JIT provision — SSO accounts get a random unusable password placeholder.
            uid = uuid.uuid4().hex
            row = auth.CpUser(
                id=uid,
                email=email,
                name=name,
                password_hash=auth.hash_password(secrets.token_urlsafe(32)),
                password_algo="argon2id",
                email_verified=True,
                created_at=time.time(),
                accepted_terms_at=time.time(),
                trial_until=0,
                subscription_json="{}",
                cohort="sso",
                support_code=_support_code(),
                account_role="owner",
                org_id=uid,
            )
            s.add(row)
        if not getattr(row, "org_id", None):
            row.org_id = row.id
        row.oidc_issuer = issuer
        row.oidc_subject = subject
        row.email_verified = True
        if name and not row.name:
            row.name = name
        s.add(row)
        user_dict = auth.user_to_dict(row)
        email_l = row.email

    _record_auth(email_l, "login.sso_success", request)
    # SSO users skip local TOTP when the IdP is trusted to enforce MFA.
    token, sid = _issue_session_for_user(email_l)
    # Prefer cookie + handoff code for browser; redirect to app.
    exchange = _analysis_code_for(sid, email_l)
    dest = oidc_auth.app_post_login_url(token_query=exchange if exchange else None)
    response = RedirectResponse(dest, status_code=302)
    set_session_cookie(response, token, max_age=SESSION_TTL_S)
    return response


# ── Admin panel (account owner/admin) ────────────────────────────────────────


def _require_account_admin(authorization: str | None, request: Request) -> dict[str, Any]:
    import auth_store as auth

    user, token, data = _session_user(authorization, request)
    if not auth.is_account_admin(user):
        raise HTTPException(403, "Account admin access is required.")
    return user


@app.get("/v1/admin/overview")
def admin_overview(request: Request, authorization: str | None = Header(default=None)):
    """Firm/account admin: seats, security posture, sessions, recent auth audit."""
    import auth_store as auth
    import oidc_auth
    from sqlalchemy import select

    user = _require_account_admin(authorization, request)
    email = user["email"]
    _record_auth(email, "admin.settings_view", request)
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        oid = auth.ensure_org_id(s, row)
        sessions = auth.list_sessions_for_user(s, row.id)
        audit = auth.list_account_audit_events(s, email, limit=50)
        user_dict = auth.user_to_dict(row)
        owner = auth.billing_owner(s, row)
        owner_dict = auth.user_to_dict(owner)
        user_dict["_billing"] = owner_dict
        user_dict["subscription"] = owner_dict.get("subscription") or {}
        user_dict["trial_until"] = owner_dict.get("trial_until")
        user_dict["stripe_customer_id"] = owner_dict.get("stripe_customer_id")
        members = auth.list_org_members(s, oid)
        pending = auth.list_pending_invites(s, oid)
        seats_used = auth.count_org_seats_used(s, oid)
        users_out = [
            {
                "user_id": m.id,
                "email": m.email,
                "role": (m.account_role or "owner"),
                "totp_enabled": bool(getattr(m, "totp_enabled", False)),
                "oidc_linked": bool(getattr(m, "oidc_subject", None)),
            }
            for m in members
        ]
        invites_out = [
            {
                "id": inv.id,
                "email": inv.email,
                "role": inv.role,
                "expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(inv.expires_at or 0))
                ),
            }
            for inv in pending
        ]
    ent = _entitlement_payload(user_dict)
    seats_n = ent.get("seats")
    return {
        "account": {
            "email": email,
            "name": user_dict.get("name") or "",
            "role": user_dict.get("account_role") or "owner",
            "support_code": user_dict.get("support_code"),
            "seats": seats_n,
            "seats_used": seats_used,
            "billing_state": ent.get("billing_state"),
            "plan": ent.get("plan"),
        },
        "security": {
            "totp_enabled": bool(user_dict.get("totp_enabled")),
            "oidc_linked": bool(user_dict.get("oidc_linked")),
            "sso_available": oidc_auth.oidc_configured(),
            "oidc_trusts_idp_mfa": oidc_auth.trusts_idp_mfa(),
            "sso_preferred": bool(user_dict.get("sso_preferred")),
        },
        "sessions": sessions,
        "audit": audit,
        "users": users_out,
        "invites": invites_out,
        "message": (
            f"Seats: {seats_used} used"
            + (f" of {seats_n}" if seats_n is not None else " (set quantity in Stripe billing)")
            + ". Invite members below; roles control Admin access."
        ),
    }


class SeatInviteBody(BaseModel):
    email: EmailStr
    role: str = "member"


class MemberRoleBody(BaseModel):
    role: str


@app.post("/v1/admin/invites")
def admin_create_invite(
    body: SeatInviteBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Owner/admin: invite a seat. Returns invite_url + token (email when mailer configured)."""
    import auth_store as auth
    from sqlalchemy import select

    user = _require_account_admin(authorization, request)
    email = user["email"]
    invite_email = body.email.lower().strip()
    role = (body.role or "member").strip().lower()
    if role not in {"admin", "member"}:
        raise HTTPException(400, "Role must be admin or member.")
    if invite_email == email:
        raise HTTPException(400, "You cannot invite yourself.")
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        actor_role = (row.account_role or "owner").strip().lower()
        if role == "admin" and actor_role != "owner":
            raise HTTPException(403, "Only the account owner can invite admins.")
        oid = auth.ensure_org_id(s, row)
        owner = auth.billing_owner(s, row)
        owner_dict = auth.user_to_dict(owner)
        sub = owner_dict.get("subscription") or {}
        try:
            seats_n = int(sub.get("seats")) if sub.get("seats") is not None else None
        except (TypeError, ValueError):
            seats_n = None
        used = auth.count_org_seats_used(s, oid)
        if seats_n is not None and used >= seats_n:
            raise HTTPException(
                403,
                f"All {seats_n} seat(s) are in use. Increase quantity in Stripe billing.",
            )
        existing = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == invite_email)
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "That email already has an account.")
        for pending in auth.list_pending_invites(s, oid):
            if pending.email == invite_email:
                raise HTTPException(409, "An invite is already pending for that email.")
        invite, raw = auth.create_seat_invite(
            s, org_id=oid, email=invite_email, role=role, invited_by=row.id
        )
        invite_id = invite.id
        app_base = (os.environ.get("CASESIFTER_PUBLIC_URL") or "http://127.0.0.1:5173").rstrip("/")
        invite_url = f"{app_base}/?seat_invite={raw}"
        mailed = False
        if mailer.mailer_configured():
            mailed = mailer.send_auth_email(
                invite_email,
                "You are invited to CaseSifter",
                (
                    f"{email} invited you to join their CaseSifter firm as {role}.\n\n"
                    f"Open this link to create your account:\n{invite_url}\n\n"
                    "This invite expires in 7 days."
                ),
            )
    _record_auth(email, "admin.invite", request, target=invite_email)
    out: dict[str, Any] = {
        "ok": True,
        "email": invite_email,
        "role": role,
        "invite_id": invite_id,
        "invite_url": invite_url,
        "mailed": mailed,
    }
    if not mailed:
        out["invite_token"] = raw
    return out


@app.delete("/v1/admin/invites/{invite_id}")
def admin_revoke_invite(
    invite_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    import auth_store as auth
    from sqlalchemy import select

    user = _require_account_admin(authorization, request)
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == user["email"])).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        oid = auth.ensure_org_id(s, row)
        inv = s.get(auth.CpSeatInvite, invite_id)
        if inv is None or inv.org_id != oid:
            raise HTTPException(404, "Invite not found.")
        inv.revoked_at = time.time()
        s.add(inv)
    _record_auth(user["email"], "admin.invite_revoke", request, target=invite_id)
    return {"ok": True}


@app.patch("/v1/admin/users/{user_id}")
def admin_set_member_role(
    user_id: str,
    body: MemberRoleBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Assign owner/admin/member. Owner-only for admin/owner grants; cannot demote last owner."""
    import auth_store as auth
    from sqlalchemy import select

    actor = _require_account_admin(authorization, request)
    role = (body.role or "").strip().lower()
    if role not in {"owner", "admin", "member"}:
        raise HTTPException(400, "Role must be owner, admin, or member.")
    _ensure_auth_db()
    with auth.session_scope() as s:
        actor_row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == actor["email"])
        ).scalar_one_or_none()
        if actor_row is None:
            raise HTTPException(401, "Unknown user.")
        oid = auth.ensure_org_id(s, actor_row)
        target = s.get(auth.CpUser, user_id)
        if target is None or auth.org_id_of(target) != oid:
            raise HTTPException(404, "Member not found.")
        actor_role = (actor_row.account_role or "owner").lower()
        target_role = (target.account_role or "owner").lower()
        if role in {"owner", "admin"} and actor_role != "owner":
            raise HTTPException(403, "Only the account owner can grant owner or admin.")
        if target_role in {"owner", "admin"} and actor_role != "owner":
            raise HTTPException(403, "Only the account owner can change owner or admin roles.")
        if target_role == "owner" and role != "owner":
            if auth.count_org_owners(s, oid) <= 1:
                raise HTTPException(400, "Cannot demote the last owner.")
        if target.id == actor_row.id and role == "member" and actor_role == "owner":
            raise HTTPException(400, "Transfer ownership to another user before leaving owner.")
        if role == "owner":
            members = list(auth.list_org_members(s, oid))
            for m in members:
                if m.id != target.id and (m.account_role or "").lower() == "owner":
                    m.account_role = "admin"
                    s.add(m)
            for m in members:
                if m.id != target.id:
                    m.org_id = target.id
                    s.add(m)
            target.org_id = target.id
        target.account_role = role
        s.add(target)
    _record_auth(actor["email"], "admin.role_change", request, target=f"{user_id}:{role}")
    return {"ok": True, "user_id": user_id, "role": role}


@app.delete("/v1/admin/users/{user_id}")
def admin_remove_member(
    user_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Detach a seat from the org (solo account) and revoke their sessions."""
    import auth_store as auth
    from sqlalchemy import select

    actor = _require_account_admin(authorization, request)
    _ensure_auth_db()
    with auth.session_scope() as s:
        actor_row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == actor["email"])
        ).scalar_one_or_none()
        if actor_row is None:
            raise HTTPException(401, "Unknown user.")
        oid = auth.ensure_org_id(s, actor_row)
        target = s.get(auth.CpUser, user_id)
        if target is None or auth.org_id_of(target) != oid:
            raise HTTPException(404, "Member not found.")
        if target.id == actor_row.id:
            raise HTTPException(400, "You cannot remove yourself. Transfer ownership first.")
        actor_role = (actor_row.account_role or "owner").lower()
        target_role = (target.account_role or "owner").lower()
        if target_role in {"owner", "admin"} and actor_role != "owner":
            raise HTTPException(403, "Only the account owner can remove an owner or admin.")
        if target_role == "owner" and auth.count_org_owners(s, oid) <= 1:
            raise HTTPException(400, "Cannot remove the last owner.")
        email_t = target.email
        auth.detach_org_member(s, target)
    _record_auth(actor["email"], "admin.member_remove", request, target=email_t)
    return {"ok": True, "user_id": user_id}


@app.post("/v1/admin/users/{user_id}/sessions/revoke")
def admin_revoke_member_sessions(
    user_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Revoke all sessions for another org member (or self)."""
    import auth_store as auth
    from sqlalchemy import select

    actor = _require_account_admin(authorization, request)
    _ensure_auth_db()
    with auth.session_scope() as s:
        actor_row = s.execute(
            select(auth.CpUser).where(auth.CpUser.email == actor["email"])
        ).scalar_one_or_none()
        if actor_row is None:
            raise HTTPException(401, "Unknown user.")
        oid = auth.ensure_org_id(s, actor_row)
        target = s.get(auth.CpUser, user_id)
        if target is None or auth.org_id_of(target) != oid:
            raise HTTPException(404, "Member not found.")
        except_sid = None
        if target.id == actor_row.id:
            user, token, _ = _session_user(authorization, request)
            parsed = _parse_session_token(token)
            except_sid = (parsed or {}).get("sid")
        count = auth.revoke_all_sessions_for_user(s, target.id, except_sid=except_sid)
    _record_auth(actor["email"], "session.revoke_member", request, target=f"{user_id}:{count}")
    return {"ok": True, "revoked": count}


class AdminSettingsBody(BaseModel):
    sso_preferred: bool | None = None


@app.patch("/v1/admin/settings")
def admin_settings(
    body: AdminSettingsBody,
    request: Request,
    authorization: str | None = Header(default=None),
):
    import auth_store as auth
    from sqlalchemy import select

    user = _require_account_admin(authorization, request)
    email = user["email"]
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        if body.sso_preferred is not None:
            row.sso_preferred = bool(body.sso_preferred)
        s.add(row)
        out = auth.user_to_dict(row)
    _record_auth(email, "admin.settings_update", request, target="sso_preferred")
    return {"ok": True, "sso_preferred": out.get("sso_preferred")}


@app.get("/v1/admin/audit")
def admin_audit(request: Request, authorization: str | None = Header(default=None)):
    import auth_store as auth

    user = _require_account_admin(authorization, request)
    _ensure_auth_db()
    with auth.session_scope() as s:
        events = auth.list_account_audit_events(s, user.get("email") or "", limit=100)
    return {"events": events}


@app.post("/v1/admin/sessions/{session_id}/revoke")
def admin_revoke_session(
    session_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    import auth_store as auth
    from sqlalchemy import select

    user = _require_account_admin(authorization, request)
    email = user["email"]
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        sess = s.get(auth.CpSession, session_id)
        if sess is None or sess.user_id != row.id:
            raise HTTPException(404, "Session not found.")
        auth.revoke_session(s, session_id)
    _record_auth(email, "session.revoke", request, target=session_id[:16])
    return {"ok": True}


@app.post("/v1/admin/sessions/revoke-others")
def admin_revoke_other_sessions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    import auth_store as auth
    from sqlalchemy import select

    user, token, _ = _session_user(authorization, request)
    if not auth.is_account_admin(user):
        raise HTTPException(403, "Account admin access is required.")
    email = user["email"]
    parsed = _parse_session_token(token)
    current_sid = (parsed or {}).get("sid")
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        count = auth.revoke_all_sessions_for_user(s, row.id, except_sid=current_sid)
    _record_auth(email, "session.revoke_all", request, target=str(count))
    return {"ok": True, "revoked": count}


@app.post("/v1/admin/sessions/revoke-firm")
def admin_revoke_firm_sessions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Owner-only: revoke all sessions for every org member except the current session."""
    import auth_store as auth
    from sqlalchemy import select

    user, token, _ = _session_user(authorization, request)
    if not auth.is_account_admin(user):
        raise HTTPException(403, "Account admin access is required.")
    email = user["email"]
    parsed = _parse_session_token(token)
    current_sid = (parsed or {}).get("sid")
    _ensure_auth_db()
    with auth.session_scope() as s:
        row = s.execute(select(auth.CpUser).where(auth.CpUser.email == email)).scalar_one_or_none()
        if row is None:
            raise HTTPException(401, "Unknown user.")
        if (row.account_role or "owner").strip().lower() != "owner":
            raise HTTPException(403, "Only the account owner can revoke firm-wide sessions.")
        oid = auth.ensure_org_id(s, row)
        count = 0
        for member in auth.list_org_members(s, oid):
            except_sid = current_sid if member.id == row.id else None
            count += auth.revoke_all_sessions_for_user(s, member.id, except_sid=except_sid)
    _record_auth(email, "session.revoke_firm", request, target=str(count))
    return {"ok": True, "revoked": count}

