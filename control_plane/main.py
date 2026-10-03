"""CaseSifter control plane — Stripe billing + entitlement (no document vault)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

DATA = Path(__file__).resolve().parent / "data"
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


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class SignupBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
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


def _load() -> dict[str, Any]:
    if not STORE.exists():
        return {"users": {}, "sessions": {}, "trials": {}}
    return json.loads(STORE.read_text(encoding="utf-8"))


def _save(data: dict[str, Any]) -> None:
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return salt, digest


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
    return {
        **_user_plan(user),
        "email": user["email"],
        "support_code": user.get("support_code"),
        "user_id": user.get("user_id"),
        "cohort": user.get("cohort"),
    }


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
    if status in {"active", "trialing"}:
        return {
            "active": True,
            "plan": sub.get("plan") or "pro",
            "message": "Subscription active.",
            "renews_at": sub.get("current_period_end"),
        }
    if trial_until > now:
        days = int((trial_until - now) / 86400)
        return {
            "active": True,
            "plan": "trial",
            "message": f"Trial active — {days} day(s) left.",
            "trial_until": trial_until,
        }
    return {
        "active": False,
        "plan": None,
        "message": "Subscribe or start a trial to analyze files.",
    }


app = FastAPI(title="CaseSifter Control Plane", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    # Checkout requires both secret + price id; expose that as stripe readiness.
    return {"status": "ok", "stripe": bool(STRIPE_SECRET and STRIPE_PRICE_ID)}


@app.post("/v1/signup")
def signup(body: SignupBody):
    if not body.accepted_terms:
        raise HTTPException(422, "You must agree to the Terms of Service and Privacy Policy to continue.")
    if not _invite_ok(body.invite_code):
        raise HTTPException(403, "A valid invite code is required for beta signup.")
    data = _load()
    email = body.email.lower()
    if email in data["users"]:
        raise HTTPException(409, "Account already exists.")
    salt, hashed = _hash_password(body.password)
    cohort = "beta" if (body.invite_code or INVITE_REQUIRED) else "general"
    data["users"][email] = {
        "email": email,
        "name": body.name or "",
        "salt": salt,
        "password_hash": hashed,
        "created_at": time.time(),
        "accepted_terms_at": time.time(),
        "trial_until": 0,
        "subscription": {},
        "cohort": cohort,
    }
    _ensure_user_identity(data["users"][email], cohort=cohort)
    token = secrets.token_urlsafe(32)
    data["sessions"][token] = {"email": email, "created_at": time.time()}
    _save(data)
    return {"token": token, "entitlement": _entitlement_payload(data["users"][email])}


@app.post("/v1/login")
def login(body: LoginBody):
    data = _load()
    email = body.email.lower()
    user = data["users"].get(email)
    if not user:
        raise HTTPException(401, "Invalid email or password.")
    _, hashed = _hash_password(body.password, user["salt"])
    if not hmac.compare_digest(hashed, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password.")
    _ensure_user_identity(user)
    data["users"][email] = user
    token = secrets.token_urlsafe(32)
    data["sessions"][token] = {"email": email, "created_at": time.time()}
    _save(data)
    return {"token": token, "entitlement": _entitlement_payload(user)}


def _session_user(authorization: str | None) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required.")
    token = authorization[7:].strip()
    data = _load()
    session = data["sessions"].get(token)
    if not session:
        raise HTTPException(401, "Invalid session.")
    user = data["users"].get(session["email"])
    if not user:
        raise HTTPException(401, "Unknown user.")
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
    admin = os.environ.get("CONTROL_ADMIN_TOKEN", "")
    if not admin or body.admin_token != admin:
        raise HTTPException(403, "Admin token required.")
    data = _load()
    email = body.email.lower()
    user = data["users"].get(email)
    if not user:
        salt, hashed = _hash_password(secrets.token_urlsafe(12))
        user = {
            "email": email,
            "name": "",
            "salt": salt,
            "password_hash": hashed,
            "created_at": time.time(),
            "subscription": {},
        }
        data["users"][email] = user
    user["trial_until"] = time.time() + TRIAL_DAYS * 86400
    _ensure_user_identity(user, cohort=body.cohort or "beta")
    _save(data)
    return {
        "email": email,
        "trial_until": user["trial_until"],
        "entitlement": _entitlement_payload(user),
    }


@app.post("/v1/checkout")
def checkout(body: CheckoutBody = CheckoutBody(), authorization: str | None = Header(default=None)):
    user, _, _ = _session_user(authorization)
    if not STRIPE_SECRET or not STRIPE_PRICE_ID:
        # Dev fallback: no Stripe configured, so there's no way to collect a card.
        data = _load()
        if body.trial:
            data["users"][user["email"]]["trial_until"] = time.time() + TRIAL_DAYS * 86400
            message = "Stripe not configured — trial started without a card (local/dev)."
        else:
            data["users"][user["email"]]["subscription"] = {
                "status": "active",
                "plan": "pro-dev",
                "current_period_end": time.time() + 30 * 86400,
            }
            message = "Stripe not configured — marked Pro for local/dev."
        _save(data)
        return {
            "mode": "dev",
            "url": None,
            "message": message,
            "entitlement": _user_plan(data["users"][user["email"]]),
        }
    try:
        import stripe

        stripe.api_key = STRIPE_SECRET
        session_kwargs = dict(
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            success_url=APP_PUBLIC_URL.rstrip("/") + "/?billing=success",
            cancel_url=APP_PUBLIC_URL.rstrip("/") + "/?billing=cancel",
            customer_email=user["email"],
            client_reference_id=user["email"],
            metadata={"email": user["email"]},
        )
        if body.trial:
            # Card is still collected up front; billing simply doesn't start until the trial ends.
            session_kwargs["subscription_data"] = {"trial_period_days": TRIAL_DAYS}
        session = stripe.checkout.Session.create(**session_kwargs)
        return {"mode": "stripe", "url": session.url}
    except Exception as exc:
        raise HTTPException(502, f"Stripe checkout failed: {exc}") from exc


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
    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    email = None
    if isinstance(obj, dict):
        email = (obj.get("metadata") or {}).get("email") or obj.get("customer_email")
        if not email and obj.get("client_reference_id"):
            email = obj.get("client_reference_id")
    if not email:
        return {"ok": True, "skipped": "no email"}

    data = _load()
    user = data["users"].get(str(email).lower())
    if not user:
        return {"ok": True, "skipped": "unknown user"}

    if etype in {"checkout.session.completed", "customer.subscription.updated", "customer.subscription.created"}:
        status = obj.get("status") if isinstance(obj, dict) else "active"
        if etype.startswith("checkout"):
            status = "active"
        user["subscription"] = {
            "status": status or "active",
            "plan": "pro",
            "stripe_id": obj.get("id") if isinstance(obj, dict) else None,
            "current_period_end": obj.get("current_period_end") if isinstance(obj, dict) else None,
        }
    elif etype in {"customer.subscription.deleted"}:
        user["subscription"] = {"status": "canceled", "plan": None}
    _save(data)
    return {"ok": True}


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
