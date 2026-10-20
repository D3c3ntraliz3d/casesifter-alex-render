"""Durable auth store (SQLite locally / Postgres when DATABASE_URL is set).

Tables are prefixed cp_ so they can share the analysis Postgres without colliding
with fact-memory models.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    Boolean,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError
except ImportError:  # pragma: no cover
    PasswordHasher = None  # type: ignore[misc, assignment]
    VerifyMismatchError = Exception  # type: ignore[misc, assignment]

_ph = PasswordHasher() if PasswordHasher else None
_lock = threading.Lock()
_engine = None
_SessionLocal = None


class Base(DeclarativeBase):
    pass


class CpUser(Base):
    __tablename__ = "cp_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_algo: Mapped[str] = mapped_column(String(32), default="argon2id")
    # Legacy sha256 migration fields (nullable after upgrade).
    legacy_salt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    accepted_terms_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    trial_until: Mapped[float] = mapped_column(Float, default=0.0)
    subscription_json: Mapped[str] = mapped_column(Text, default="{}")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    support_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cohort: Mapped[str] = mapped_column(String(64), default="general")
    # Opaque claim maps (trial_domains / trial_emails live in cp_meta).
    # 2FA / SSO (added via ensure_auth_schema; defaults keep password-only accounts working)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_confirmed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    recovery_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    oidc_issuer: Mapped[str | None] = mapped_column(String(512), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Account role for control-plane admin panel (owner | admin | member).
    account_role: Mapped[str] = mapped_column(String(32), default="owner")
    # When true, password login for this email domain prefers OIDC (soft hint; env still gates).
    sso_preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    # Shared billing/org: owner's user id. Members share the owner's org_id / seats.
    org_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)


class CpSession(Base):
    __tablename__ = "cp_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CpEmailToken(Base):
    __tablename__ = "cp_email_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)  # verify | reset
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CpExchangeCode(Base):
    __tablename__ = "cp_exchange_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CpMeta(Base):
    __tablename__ = "cp_meta"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")


class CpAuthAudit(Base):
    """Sign-in and admin events. No passwords, tokens, or document bytes."""

    __tablename__ = "cp_auth_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(320), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    at: Mapped[float] = mapped_column(Float, default=0.0)


class CpMfaChallenge(Base):
    """Short-lived token after password check, before TOTP/recovery completes."""

    __tablename__ = "cp_mfa_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CpOidcState(Base):
    """CSRF state + PKCE verifier for the OIDC authorize redirect."""

    __tablename__ = "cp_oidc_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    used_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class CpSeatInvite(Base):
    """Pending seat invite for multi-user orgs (owner/admin invites member/admin)."""

    __tablename__ = "cp_seat_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), default="member")
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    invited_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    accepted_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        # SQLAlchemy wants postgresql://
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        return url
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        raise RuntimeError(
            "DATABASE_URL is required when APP_ENV=production "
            "(no SQLite fallback). Refusing to boot."
        )
    data_dir = (os.environ.get("CONTROL_DATA_DIR") or "").strip()
    root = Path(data_dir) if data_dir else (Path(__file__).resolve().parent / "data")
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(root / 'auth.sqlite3').resolve().as_posix()}"


_USER_COLUMN_DDL = (
    ("totp_secret_enc", "TEXT"),
    ("totp_enabled", "BOOLEAN DEFAULT 0"),
    ("totp_confirmed_at", "FLOAT"),
    ("recovery_codes_json", "TEXT DEFAULT '[]'"),
    ("oidc_issuer", "VARCHAR(512)"),
    ("oidc_subject", "VARCHAR(255)"),
    ("account_role", "VARCHAR(32) DEFAULT 'owner'"),
    ("sso_preferred", "BOOLEAN DEFAULT 0"),
    ("org_id", "VARCHAR(36)"),
)


def ensure_auth_schema(engine=None) -> None:
    """Add new cp_users columns on existing databases (create_all does not alter)."""
    eng = engine or _engine
    if eng is None:
        return
    pg_ddl = {
        "totp_secret_enc": "TEXT",
        "totp_enabled": "BOOLEAN DEFAULT FALSE",
        "totp_confirmed_at": "DOUBLE PRECISION",
        "recovery_codes_json": "TEXT DEFAULT '[]'",
        "oidc_issuer": "VARCHAR(512)",
        "oidc_subject": "VARCHAR(255)",
        "account_role": "VARCHAR(32) DEFAULT 'owner'",
        "sso_preferred": "BOOLEAN DEFAULT FALSE",
        "org_id": "VARCHAR(36)",
    }
    with eng.begin() as conn:
        dialect = eng.dialect.name
        if dialect == "sqlite":
            existing = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(cp_users)")).fetchall()
            }
            for name, ddl in _USER_COLUMN_DDL:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE cp_users ADD COLUMN {name} {ddl}"))
        else:
            for name, ddl in pg_ddl.items():
                conn.execute(
                    text(f"ALTER TABLE cp_users ADD COLUMN IF NOT EXISTS {name} {ddl}")
                )


def init_auth_store(*, force: bool = False) -> None:
    global _engine, _SessionLocal
    with _lock:
        if _engine is not None and not force:
            return
        url = _database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
        Base.metadata.create_all(_engine)
        ensure_auth_schema(_engine)


def reset_auth_store_for_tests() -> None:
    global _engine, _SessionLocal
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _SessionLocal = None


@contextmanager
def session_scope() -> Iterator[Session]:
    init_auth_store()
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def hash_password(password: str) -> str:
    if _ph is None:
        raise RuntimeError("argon2-cffi is required")
    return _ph.hash(password)


def verify_password(user: CpUser, password: str) -> bool:
    algo = (user.password_algo or "argon2id").lower()
    if algo == "argon2id":
        if _ph is None:
            return False
        try:
            return bool(_ph.verify(user.password_hash, password))
        except VerifyMismatchError:
            return False
        except Exception:
            return False
    # Legacy sha256(salt:password)
    salt = user.legacy_salt or ""
    import hashlib
    import hmac as hm

    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return hm.compare_digest(digest, user.password_hash or "")


def upgrade_password_if_needed(s: Session, user: CpUser, password: str) -> None:
    if (user.password_algo or "").lower() == "argon2id":
        return
    user.password_hash = hash_password(password)
    user.password_algo = "argon2id"
    user.legacy_salt = None
    s.add(user)


def user_to_dict(user: CpUser) -> dict[str, Any]:
    try:
        sub = json.loads(user.subscription_json or "{}")
    except json.JSONDecodeError:
        sub = {}
    org_id = getattr(user, "org_id", None) or user.id
    return {
        "email": user.email,
        "name": user.name or "",
        "user_id": user.id,
        "org_id": org_id,
        "support_code": user.support_code,
        "cohort": user.cohort or "general",
        "trial_until": float(user.trial_until or 0),
        "subscription": sub if isinstance(sub, dict) else {},
        "stripe_customer_id": user.stripe_customer_id,
        "email_verified": bool(user.email_verified),
        "created_at": float(user.created_at or 0),
        "accepted_terms_at": user.accepted_terms_at,
        "totp_enabled": bool(getattr(user, "totp_enabled", False)),
        "oidc_linked": bool(getattr(user, "oidc_subject", None)),
        "account_role": (getattr(user, "account_role", None) or "owner"),
        "sso_preferred": bool(getattr(user, "sso_preferred", False)),
    }


def apply_user_dict(user: CpUser, data: dict[str, Any]) -> None:
    if "name" in data:
        user.name = str(data.get("name") or "")
    if "trial_until" in data:
        user.trial_until = float(data.get("trial_until") or 0)
    if "subscription" in data:
        user.subscription_json = json.dumps(data.get("subscription") or {})
    if "stripe_customer_id" in data:
        user.stripe_customer_id = data.get("stripe_customer_id")
    if "support_code" in data:
        user.support_code = data.get("support_code")
    if "cohort" in data:
        user.cohort = str(data.get("cohort") or "general")
    if "email_verified" in data:
        user.email_verified = bool(data.get("email_verified"))


def get_meta(s: Session, key: str) -> dict[str, Any]:
    row = s.get(CpMeta, key)
    if not row:
        return {}
    try:
        val = json.loads(row.value_json or "{}")
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def set_meta(s: Session, key: str, value: dict[str, Any]) -> None:
    row = s.get(CpMeta, key)
    if row is None:
        row = CpMeta(key=key, value_json=json.dumps(value))
        s.add(row)
    else:
        row.value_json = json.dumps(value)


def migrate_accounts_json(path: Path) -> int:
    """Import missing users from legacy accounts.json into cp_users.

    Preserves SHA-256 password_hash + salt (password_algo=sha256) so the next
    successful login upgrades to argon2id via upgrade_password_if_needed.
    Existing accounts are marked email_verified=True (grandfather).
    Idempotent: skips emails already present in cp_users.
    """
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    users = data.get("users") or {}
    if not users:
        return 0
    imported = 0
    with session_scope() as s:
        for email, u in users.items():
            email_n = str(email).lower().strip()
            if not email_n:
                continue
            exists = s.execute(
                select(CpUser).where(CpUser.email == email_n)
            ).scalar_one_or_none()
            if exists is not None:
                continue
            uid = str(u.get("user_id") or uuid.uuid4().hex)
            # Prefer stable control-plane user_id; fall back to hex.
            row = CpUser(
                id=uid if len(uid) >= 8 else uuid.uuid4().hex,
                email=email_n,
                name=str(u.get("name") or ""),
                password_hash=str(u.get("password_hash") or ""),
                password_algo="sha256",
                legacy_salt=str(u.get("salt") or ""),
                email_verified=True,  # grandfather existing accounts
                created_at=float(u.get("created_at") or time.time()),
                accepted_terms_at=u.get("accepted_terms_at"),
                trial_until=float(u.get("trial_until") or 0),
                subscription_json=json.dumps(u.get("subscription") or {}),
                stripe_customer_id=u.get("stripe_customer_id"),
                support_code=u.get("support_code"),
                cohort=str(u.get("cohort") or "general"),
            )
            s.add(row)
            imported += 1
        # Merge trial claim maps (do not wipe newer SQL state).
        for key in ("trial_domains", "trial_emails"):
            incoming = data.get(key) or {}
            if not isinstance(incoming, dict) or not incoming:
                continue
            current = get_meta(s, key)
            current.update(incoming)
            set_meta(s, key, current)
        # Keep accounts.json in place (never delete/rename) so a mid-deploy
        # failure can re-run migration safely; existing emails are skipped.
    return imported


def create_session(s: Session, user: CpUser, *, ttl_s: int) -> CpSession:
    now = time.time()
    row = CpSession(
        id=uuid.uuid4().hex,
        user_id=user.id,
        email=user.email,
        created_at=now,
        expires_at=now + max(60, ttl_s),
        revoked_at=None,
    )
    s.add(row)
    return row


def get_active_session(s: Session, session_id: str) -> CpSession | None:
    row = s.get(CpSession, session_id)
    if row is None:
        return None
    if row.revoked_at is not None:
        return None
    if row.expires_at and row.expires_at < time.time():
        return None
    return row


def revoke_session(s: Session, session_id: str) -> bool:
    row = s.get(CpSession, session_id)
    if row is None:
        return False
    if row.revoked_at is None:
        row.revoked_at = time.time()
        s.add(row)
    return True


def create_email_token(
    s: Session, user: CpUser, *, purpose: str, ttl_s: int = 3600
) -> str:
    raw = secrets.token_urlsafe(32)
    import hashlib

    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = time.time()
    s.add(
        CpEmailToken(
            id=uuid.uuid4().hex,
            user_id=user.id,
            email=user.email,
            purpose=purpose,
            token_hash=th,
            created_at=now,
            expires_at=now + ttl_s,
        )
    )
    return raw


def consume_email_token(s: Session, raw: str, *, purpose: str) -> CpUser | None:
    import hashlib

    th = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()
    row = s.execute(
        select(CpEmailToken).where(
            CpEmailToken.token_hash == th,
            CpEmailToken.purpose == purpose,
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < time.time():
        return None
    user = s.get(CpUser, row.user_id)
    if user is None:
        return None
    row.used_at = time.time()
    s.add(row)
    return user


def create_exchange_code(s: Session, session_id: str, email: str) -> str:
    raw = secrets.token_urlsafe(24)
    import hashlib

    th = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = time.time()
    s.add(
        CpExchangeCode(
            id=uuid.uuid4().hex,
            code_hash=th,
            session_id=session_id,
            email=email,
            created_at=now,
            expires_at=now + 60,
        )
    )
    return raw


def consume_exchange_code(s: Session, raw: str) -> CpSession | None:
    import hashlib

    th = hashlib.sha256((raw or "").encode("utf-8")).hexdigest()
    row = s.execute(select(CpExchangeCode).where(CpExchangeCode.code_hash == th)).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < time.time():
        return None
    sess = get_active_session(s, row.session_id)
    if sess is None:
        return None
    row.used_at = time.time()
    s.add(row)
    return sess


def record_auth_audit(
    s: Session,
    *,
    actor: str,
    action: str,
    target: str | None = None,
    request_id: str | None = None,
) -> None:
    s.add(
        CpAuthAudit(
            actor=(actor or "")[:320],
            action=(action or "")[:64],
            target=(target[:320] if target else None),
            request_id=((request_id or "")[:64] or None),
            at=time.time(),
        )
    )


_ACCOUNT_AUDIT_ACTIONS = (
    "login.success",
    "login.failure",
    "login.mfa_required",
    "login.mfa_failure",
    "login.sso_success",
    "login.sso_failure",
    "2fa.enroll_start",
    "2fa.enroll_confirm",
    "2fa.disable",
    "2fa.recovery_used",
    "session.revoke",
    "session.revoke_all",
    "session.revoke_member",
    "session.revoke_firm",
    "admin.settings_view",
    "admin.settings_update",
    "admin.invite",
    "admin.invite_revoke",
    "admin.role_change",
    "admin.member_remove",
)


def list_sign_in_events(s: Session, actor: str, *, limit: int = 50) -> list[dict]:
    """Sign-in rows for this account only. Admin grants stay off this list."""
    email = (actor or "").strip().lower()
    if not email:
        return []
    rows = s.execute(
        select(CpAuthAudit)
        .where(CpAuthAudit.actor == email)
        .where(CpAuthAudit.action.in_(("login.success", "login.failure")))
        .order_by(CpAuthAudit.id.desc())
        .limit(max(1, min(int(limit), 100)))
    ).scalars().all()
    return [
        {
            "action": row.action,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row.at or 0)),
            "request_id": row.request_id,
        }
        for row in rows
    ]


def list_account_audit_events(s: Session, actor: str, *, limit: int = 100) -> list[dict]:
    """Broader auth/admin events for the signed-in account (still actor-scoped)."""
    email = (actor or "").strip().lower()
    if not email:
        return []
    rows = s.execute(
        select(CpAuthAudit)
        .where(CpAuthAudit.actor == email)
        .where(CpAuthAudit.action.in_(_ACCOUNT_AUDIT_ACTIONS))
        .order_by(CpAuthAudit.id.desc())
        .limit(max(1, min(int(limit), 200)))
    ).scalars().all()
    return [
        {
            "action": row.action,
            "target": row.target,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row.at or 0)),
            "request_id": row.request_id,
        }
        for row in rows
    ]


def _token_hash(raw: str) -> str:
    import hashlib

    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def create_mfa_challenge(s: Session, user: CpUser, *, ttl_s: int = 300) -> str:
    raw = secrets.token_urlsafe(32)
    now = time.time()
    s.add(
        CpMfaChallenge(
            id=uuid.uuid4().hex,
            user_id=user.id,
            email=user.email,
            token_hash=_token_hash(raw),
            created_at=now,
            expires_at=now + max(60, ttl_s),
        )
    )
    return raw


def consume_mfa_challenge(s: Session, raw: str) -> CpUser | None:
    row = s.execute(
        select(CpMfaChallenge).where(CpMfaChallenge.token_hash == _token_hash(raw))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < time.time():
        return None
    user = s.get(CpUser, row.user_id)
    if user is None:
        return None
    row.used_at = time.time()
    s.add(row)
    return user


def create_oidc_state(s: Session, *, nonce: str, code_verifier: str, ttl_s: int = 600) -> str:
    raw = secrets.token_urlsafe(24)
    now = time.time()
    s.add(
        CpOidcState(
            id=uuid.uuid4().hex,
            state_hash=_token_hash(raw),
            nonce=nonce,
            code_verifier=code_verifier,
            created_at=now,
            expires_at=now + max(60, ttl_s),
        )
    )
    return raw


def consume_oidc_state(s: Session, raw: str) -> CpOidcState | None:
    row = s.execute(
        select(CpOidcState).where(CpOidcState.state_hash == _token_hash(raw))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None:
        return None
    if row.expires_at < time.time():
        return None
    row.used_at = time.time()
    s.add(row)
    return row


def list_sessions_for_user(s: Session, user_id: str) -> list[dict[str, Any]]:
    now = time.time()
    rows = s.execute(
        select(CpSession)
        .where(CpSession.user_id == user_id)
        .order_by(CpSession.created_at.desc())
        .limit(50)
    ).scalars().all()
    out = []
    for row in rows:
        active = row.revoked_at is None and (not row.expires_at or row.expires_at >= now)
        out.append(
            {
                "id": row.id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row.created_at or 0)),
                "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row.expires_at or 0)),
                "revoked": row.revoked_at is not None,
                "active": active,
            }
        )
    return out


def revoke_all_sessions_for_user(s: Session, user_id: str, *, except_sid: str | None = None) -> int:
    count = 0
    now = time.time()
    for sess in s.execute(select(CpSession).where(CpSession.user_id == user_id)).scalars():
        if except_sid and sess.id == except_sid:
            continue
        if sess.revoked_at is None:
            sess.revoked_at = now
            s.add(sess)
            count += 1
    return count


def find_user_by_oidc(s: Session, *, issuer: str, subject: str) -> CpUser | None:
    if not issuer or not subject:
        return None
    return s.execute(
        select(CpUser).where(
            CpUser.oidc_issuer == issuer,
            CpUser.oidc_subject == subject,
        )
    ).scalar_one_or_none()


def is_account_admin(user: CpUser | dict[str, Any]) -> bool:
    if isinstance(user, dict):
        role = (user.get("account_role") or "owner").strip().lower()
    else:
        role = (getattr(user, "account_role", None) or "owner").strip().lower()
    return role in {"owner", "admin"}


def org_id_of(user: CpUser) -> str:
    return (getattr(user, "org_id", None) or user.id or "").strip() or user.id


def ensure_org_id(s: Session, user: CpUser) -> str:
    """Backfill org_id to self for legacy owner rows."""
    if not getattr(user, "org_id", None):
        user.org_id = user.id
        s.add(user)
    return org_id_of(user)


def billing_owner(s: Session, user: CpUser) -> CpUser:
    """Subscription/seats live on the org owner row."""
    oid = ensure_org_id(s, user)
    if oid == user.id:
        return user
    owner = s.get(CpUser, oid)
    return owner if owner is not None else user


def list_org_members(s: Session, org_id: str) -> list[CpUser]:
    return list(
        s.execute(select(CpUser).where(CpUser.org_id == org_id).order_by(CpUser.created_at)).scalars()
    )


def count_org_owners(s: Session, org_id: str) -> int:
    return sum(
        1
        for m in list_org_members(s, org_id)
        if (m.account_role or "owner").strip().lower() == "owner"
    )


def detach_org_member(s: Session, user: CpUser) -> None:
    """Remove a user from a shared org: solo firm + revoke all sessions."""
    user.org_id = user.id
    user.account_role = "owner"
    s.add(user)
    revoke_all_sessions_for_user(s, user.id)


def count_org_seats_used(s: Session, org_id: str) -> int:
    members = list_org_members(s, org_id)
    pending = list(
        s.execute(
            select(CpSeatInvite).where(
                CpSeatInvite.org_id == org_id,
                CpSeatInvite.accepted_at.is_(None),
                CpSeatInvite.revoked_at.is_(None),
                CpSeatInvite.expires_at > time.time(),
            )
        ).scalars()
    )
    return len(members) + len(pending)


def create_seat_invite(
    s: Session,
    *,
    org_id: str,
    email: str,
    role: str,
    invited_by: str,
    ttl_s: int = 7 * 24 * 3600,
) -> tuple[CpSeatInvite, str]:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = time.time()
    invite = CpSeatInvite(
        id=str(uuid.uuid4()),
        org_id=org_id,
        email=(email or "").strip().lower(),
        role=(role or "member").strip().lower() or "member",
        token_hash=token_hash,
        invited_by=invited_by,
        created_at=now,
        expires_at=now + ttl_s,
    )
    s.add(invite)
    return invite, raw


def find_seat_invite_by_token(s: Session, raw_token: str) -> CpSeatInvite | None:
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    row = s.execute(
        select(CpSeatInvite).where(CpSeatInvite.token_hash == token_hash)
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.revoked_at or row.accepted_at:
        return None
    if float(row.expires_at or 0) < time.time():
        return None
    return row


def list_pending_invites(s: Session, org_id: str) -> list[CpSeatInvite]:
    return list(
        s.execute(
            select(CpSeatInvite).where(
                CpSeatInvite.org_id == org_id,
                CpSeatInvite.accepted_at.is_(None),
                CpSeatInvite.revoked_at.is_(None),
                CpSeatInvite.expires_at > time.time(),
            )
        ).scalars()
    )
