"""App-level encryption for sensitive fact-memory columns.

Per-firm data keys are Fernet keys wrapped (encrypted) by a master key from
FACT_MEMORY_MASTER_KEY. When the master key is unset in development, a
deterministic local key is derived so SQLite unit tests still exercise the
encrypt/decrypt path — never use that fallback in production.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _normalize_master_material(raw: str) -> bytes:
    """Accept url-safe base64 Fernet key, hex, or arbitrary secret → 32-byte key."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty master key")
    # Already a Fernet key (44 chars url-safe b64 of 32 bytes)
    try:
        decoded = base64.urlsafe_b64decode(text.encode("ascii"))
        if len(decoded) == 32:
            return decoded
    except Exception:
        pass
    try:
        decoded = bytes.fromhex(text)
        if len(decoded) == 32:
            return decoded
    except ValueError:
        pass
    return hashlib.sha256(text.encode("utf-8")).digest()


def _is_production() -> bool:
    env = (
        os.environ.get("APP_ENV")
        or os.environ.get("ENV")
        or ""
    ).strip().lower()
    return env == "production"


@lru_cache(maxsize=1)
def master_fernet() -> Fernet:
    raw = (os.environ.get("FACT_MEMORY_MASTER_KEY") or "").strip()
    if not raw:
        if _is_production():
            raise RuntimeError(
                "FACT_MEMORY_MASTER_KEY is required when APP_ENV=production"
            )
        # Dev/test only — log once.
        logger.warning(
            "FACT_MEMORY_MASTER_KEY unset; using ephemeral-dev master key. "
            "Set a strong secret before production writes."
        )
        material = hashlib.sha256(b"casesifter-dev-fact-memory-master").digest()
    else:
        material = _normalize_master_material(raw)
    key = base64.urlsafe_b64encode(material)
    return Fernet(key)


def generate_firm_key() -> bytes:
    """Return a raw 32-byte firm data key (Fernet material)."""
    return Fernet.generate_key()  # already url-safe b64 of 32 bytes


def wrap_firm_key(firm_key: bytes) -> str:
    """Encrypt firm key with master key; return url-safe token string."""
    return master_fernet().encrypt(firm_key).decode("ascii")


def unwrap_firm_key(wrapped: str) -> bytes:
    return master_fernet().decrypt(wrapped.encode("ascii"))


def _fernet_for_secret(secret: str) -> Fernet:
    material = _normalize_master_material(secret)
    return Fernet(base64.urlsafe_b64encode(material))


def rewrap_firm_key(wrapped: str, *, old_secret: str, new_secret: str) -> str:
    """Decrypt a firm key with the old master secret and wrap it with the new one.

    Fact rows stay encrypted under the firm key. Only the wrapped key changes.
    """
    if not (old_secret or "").strip() or not (new_secret or "").strip():
        raise ValueError("both master secrets are required")
    raw = _fernet_for_secret(old_secret).decrypt(wrapped.encode("ascii"))
    return _fernet_for_secret(new_secret).encrypt(raw).decode("ascii")


def firm_fernet(wrapped_key: str) -> Fernet:
    raw = unwrap_firm_key(wrapped_key)
    # generate_firm_key returns Fernet.generate_key() which is already b64.
    if len(raw) == 44 or (isinstance(raw, bytes) and raw.endswith(b"=")):
        return Fernet(raw)
    return Fernet(base64.urlsafe_b64encode(raw if len(raw) == 32 else hashlib.sha256(raw).digest()))


def encrypt_bytes(fernet: Fernet, value: bytes | None) -> str | None:
    if value is None:
        return None
    return fernet.encrypt(value).decode("ascii")


def decrypt_bytes(fernet: Fernet, token: str | None) -> bytes | None:
    if token is None:
        return None
    try:
        return fernet.decrypt(token.encode("ascii"))
    except InvalidToken as exc:
        raise ValueError("failed to decrypt fact-memory field") from exc


def encrypt_text(fernet: Fernet, value: str | None) -> str | None:
    if value is None:
        return None
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(fernet: Fernet, token: str | None) -> str | None:
    if token is None:
        return None
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("failed to decrypt fact-memory field") from exc


def encrypt_json(fernet: Fernet, value) -> str | None:
    import json

    if value is None:
        return None
    return encrypt_text(fernet, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def decrypt_json(fernet: Fernet, token: str | None):
    import json

    text = decrypt_text(fernet, token)
    if text is None:
        return None
    return json.loads(text)
