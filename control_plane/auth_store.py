"""Durable auth store (SQLite locally / Postgres when DATABASE_URL is set).

Tables are prefixed cp_ so they can share the analysis Postgres without colliding
with fact-memory models.
"""

from __future__ import annotations

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
    return {
        "email": user.email,
        "name": user.name or "",
        "user_id": user.id,
        "support_code": user.support_code,
        "cohort": user.cohort or "general",
        "trial_until": float(user.trial_until or 0),
        "subscription": sub if isinstance(sub, dict) else {},
        "stripe_customer_id": user.stripe_customer_id,
        "email_verified": bool(user.email_verified),
        "created_at": float(user.created_at or 0),
        "accepted_terms_at": user.accepted_terms_at,
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
