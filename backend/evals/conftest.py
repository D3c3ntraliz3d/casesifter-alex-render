"""Pytest hooks for backend/evals."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: requires TEST_DATABASE_URL / DATABASE_URL pointing at Postgres",
    )
