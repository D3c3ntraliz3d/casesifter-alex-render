"""Pytest hooks for backend/evals."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: requires TEST_DATABASE_URL / DATABASE_URL pointing at Postgres",
    )
    config.addinivalue_line(
        "markers",
        "integration: optional live Gemini / network fixtures (skip cleanly without keys)",
    )


@pytest.fixture(autouse=True)
def _reset_master_key_cache():
    """Each test sets its own FACT_MEMORY_MASTER_KEY; drop the cached Fernet.

    ``master_fernet`` is ``lru_cache``d for the process lifetime (correct in
    production, where the env never changes). Without this, a test inherits
    the previous test's key in-process while spawned children (recompute)
    read the current env, and decryption fails with InvalidToken.
    """
    from app.fact_memory.encryption import master_fernet

    master_fernet.cache_clear()
    yield
    master_fernet.cache_clear()
