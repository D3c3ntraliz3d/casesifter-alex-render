"""Master-key rotation rewraps firm keys without changing fact ciphertext."""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.fact_memory.encryption import rewrap_firm_key


def test_rewrap_round_trip():
    firm_key = Fernet.generate_key()
    old = "old-master-secret-value"
    new = "new-master-secret-value"
    wrapped = rewrap_firm_key(
        # wrap with old by rewrapping a key that was wrapped... build via the helper's inverse
        __import__("app.fact_memory.encryption", fromlist=["_fernet_for_secret"])._fernet_for_secret(old).encrypt(firm_key).decode("ascii"),
        old_secret=old,
        new_secret=new,
    )
    opened = __import__("app.fact_memory.encryption", fromlist=["_fernet_for_secret"])._fernet_for_secret(new).decrypt(
        wrapped.encode("ascii")
    )
    assert opened == firm_key
