"""Image-forensics library probe (OpenCV + pdfium).

Used by /api/health (find_spec only — no heavy import) and by the module
before raster work so a missing wheel fails with a clear error.
"""

from __future__ import annotations

import importlib.util


# Import names the Image check needs. Package wheels: opencv-python-headless, pypdfium2.
_REQUIRED = ("cv2", "pypdfium2", "numpy")


def missing_image_forensics_deps() -> list[str]:
    """Return import names that are not installed. Does not load the modules."""
    missing: list[str] = []
    for name in _REQUIRED:
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def require_image_forensics_deps() -> None:
    """Raise ImportError listing missing packages, or import them to prove they load."""
    missing = missing_image_forensics_deps()
    if missing:
        raise ImportError(
            "Image forensics unavailable — install "
            + ", ".join(missing)
            + " (see backend/requirements.txt: opencv-python-headless, pypdfium2)"
        )
    # Prove the wheels load (catches broken installs find_spec still finds).
    import cv2  # noqa: F401
    import numpy  # noqa: F401
    import pypdfium2  # noqa: F401
