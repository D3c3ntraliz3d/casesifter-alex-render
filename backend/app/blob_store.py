"""Object storage for scan job bundles (local disk now; GCS in Phase 3)."""

from __future__ import annotations

import logging
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from app.config import settings

logger = logging.getLogger(__name__)


class BlobStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes | BinaryIO, *, content_type: str = "application/octet-stream") -> str:
        """Store bytes; return the storage key."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Read object bytes."""

    @abstractmethod
    def get_file(self, key: str, dest: Path) -> Path:
        """Download object to ``dest`` (streaming when possible)."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete one object. Missing keys are success."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete all keys under ``prefix``. Return count removed."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def signed_upload_url(self, key: str, *, content_type: str, expires_s: int = 3600) -> str:
        """Return a URL the client can PUT to (local: path under API)."""

    @abstractmethod
    def signed_download_url(self, key: str, *, expires_s: int = 3600) -> str:
        ...


class LocalBlobStore(BlobStore):
    """Disk folder under ``settings.blob_dir``. Dev / docker-compose / tests."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else settings.blob_dir
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Prevent path escape.
        clean = key.replace("\\", "/").lstrip("/")
        if ".." in clean.split("/"):
            raise ValueError("invalid blob key")
        path = (self.root / clean).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError("invalid blob key")
        return path

    def put(self, key: str, data: bytes | BinaryIO, *, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (bytes, bytearray)):
            path.write_bytes(data)
        else:
            with path.open("wb") as handle:
                shutil.copyfileobj(data, handle)
        # content_type retained for GCS parity; unused on disk.
        _ = content_type
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        return path.read_bytes()

    def get_file(self, key: str, dest: Path) -> Path:
        path = self._path(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        return dest

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return True
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return True
        try:
            path.unlink()
        except OSError:
            return False
        return True

    def delete_prefix(self, prefix: str) -> int:
        target = self._path(prefix.rstrip("/")) if prefix else self.root
        if not target.exists():
            return 0
        if target.is_file():
            target.unlink(missing_ok=True)
            return 1
        count = sum(1 for p in target.rglob("*") if p.is_file())
        shutil.rmtree(target, ignore_errors=True)
        return count

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def signed_upload_url(self, key: str, *, content_type: str, expires_s: int = 3600) -> str:
        # Local: client PUTs to the analysis API local-upload route.
        base = (settings.public_analysis_base_url or "http://127.0.0.1:8000").rstrip("/")
        exp = int(time.time()) + max(60, int(expires_s))
        return (
            f"{base}/api/internal/v1/local-blobs/{quote(key, safe='/')}"
            f"?expires={exp}&content_type={quote(content_type)}"
        )

    def signed_download_url(self, key: str, *, expires_s: int = 3600) -> str:
        return self.signed_upload_url(key, content_type="application/octet-stream", expires_s=expires_s)


class GcsBlobStore(BlobStore):
    """Placeholder for Phase 3 — not used until SCAN_BACKEND=cloudrun."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("GCS blob store lands in Phase 3 (deploy/gcp).")


_store: BlobStore | None = None


def get_blob_store() -> BlobStore:
    global _store
    if _store is not None:
        return _store
    backend = (settings.blob_backend or "local").strip().lower()
    if backend == "gcs":
        _store = GcsBlobStore()
    else:
        _store = LocalBlobStore()
    return _store


def reset_blob_store_for_tests(store: BlobStore | None = None) -> None:
    global _store
    _store = store


def job_bundle_prefix(job_id: str) -> str:
    return f"jobs/{job_id}"


def job_source_key(job_id: str, source_name: str) -> str:
    safe = Path(source_name).name or "document.pdf"
    return f"{job_bundle_prefix(job_id)}/{safe}"


def job_meta_key(job_id: str) -> str:
    return f"{job_bundle_prefix(job_id)}/job.json"
