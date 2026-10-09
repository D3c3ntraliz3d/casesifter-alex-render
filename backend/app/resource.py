"""Lightweight process resource snapshots (no heavy deps).

On Linux (Render) reads /proc/self/status. Elsewhere returns None for RSS.
"""

from __future__ import annotations

from pathlib import Path


def rss_mb() -> float | None:
    """Resident set size in megabytes, or None if unavailable."""
    status = Path("/proc/self/status")
    try:
        for line in status.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    return round(int(parts[1]) / 1024.0, 1)
    except (OSError, ValueError):
        return None
    return None
