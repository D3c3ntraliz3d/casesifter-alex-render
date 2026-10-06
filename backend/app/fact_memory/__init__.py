"""Firm-scoped persistent fact memory (no PDF bytes).

Reuses vault compare_to_case snapshot shapes. Sensitive columns are encrypted
at the app layer with a per-firm key wrapped by FACT_MEMORY_MASTER_KEY.
"""

from __future__ import annotations

from app.fact_memory.store import FactMemoryStore, get_store

__all__ = ["FactMemoryStore", "get_store"]
