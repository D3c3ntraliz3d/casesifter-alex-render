"""Practice packs — element maps and ordinary-dispute suppresses (not gotcha keyword forks)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings

_PACK_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=8)
def load_pack(name: str | None = None) -> dict[str, Any]:
    pack = (name or settings.practice_pack or "foreclosure").strip().lower()
    path = _PACK_DIR / f"{pack}.json"
    if not path.exists():
        path = _PACK_DIR / "foreclosure.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["_id"] = path.stem
    return data


def pack_verify_appendix(name: str | None = None) -> str:
    """Extra verify-prompt context from the active practice pack."""
    pack = load_pack(name)
    lines: list[str] = [f"Active practice pack: {pack.get('label') or pack.get('_id')}."]
    elements = pack.get("element_cues") or []
    if elements:
        lines.append("Materiality element/procedure cues for this practice include: " + ", ".join(elements[:40]) + ".")
    ordinary = pack.get("ordinary_dispute_examples") or []
    if ordinary:
        lines.append("Treat these as ordinary dispute (suppress), not gotchas:")
        for ex in ordinary[:12]:
            lines.append(f"- {ex}")
    return "\n".join(lines)


def pack_impact_cues(name: str | None = None) -> list[str]:
    pack = load_pack(name)
    return [str(x).lower() for x in (pack.get("element_cues") or []) if x]


def pack_high_value_exhibits(name: str | None = None) -> list[str]:
    pack = load_pack(name)
    return [str(x).lower() for x in (pack.get("high_value_exhibit_kinds") or []) if x]
