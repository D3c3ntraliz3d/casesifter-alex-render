from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.models import Finding, ModuleResult


def timed_module(name: str, fn: Callable[[], tuple[list[Finding], dict[str, Any]]]) -> ModuleResult:
    started = time.perf_counter()
    try:
        findings, extras = fn()
        duration = int((time.perf_counter() - started) * 1000)
        return ModuleResult(
            name=name,
            status="ok",
            duration_ms=duration,
            summary=f"{len(findings)} finding(s)",
            findings=findings,
            extras=extras,
        )
    except Exception as exc:
        duration = int((time.perf_counter() - started) * 1000)
        return ModuleResult(
            name=name,
            status="error",
            duration_ms=duration,
            summary=f"Module failed: {exc}",
            findings=[],
            extras={"error": str(exc)},
        )


def finding(
    *,
    fid: str,
    category: str,
    severity: str,
    title: str,
    explanation: str,
    why_suspicious: str,
    confidence: float,
    page: int | None = None,
    evidence: dict | None = None,
    recommended_action: str | None = None,
) -> Finding:
    return Finding(
        id=fid,
        category=category,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        title=title,
        explanation=explanation,
        why_suspicious=why_suspicious,
        confidence=round(min(100.0, max(0.0, confidence)), 1),
        page=page,
        evidence=evidence or {},
        recommended_action=recommended_action,
    )
