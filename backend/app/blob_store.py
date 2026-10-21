"""Object storage for scan job bundles (local disk, or S3-compatible: R2 / AWS S3)."""

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


class S3BlobStore(BlobStore):
    """S3-compatible object storage. Cloudflare R2 by default; AWS S3 by config only.

    Switching providers is environment-only: set S3_ENDPOINT_URL (R2) or leave it
    unset with a real S3_REGION (AWS). Same API, same code path.
    """

    def __init__(self, client=None, bucket: str | None = None) -> None:
        self.bucket = (bucket or settings.s3_bucket or "").strip()
        if not self.bucket:
            raise RuntimeError("S3_BUCKET is required when BLOB_BACKEND=s3")
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=(settings.s3_endpoint_url or None),
                region_name=(settings.s3_region or "auto"),
                aws_access_key_id=settings.s3_access_key_id or None,
                aws_secret_access_key=settings.s3_secret_access_key or None,
                config=Config(
                    signature_version="s3v4",
                    retries={"max_attempts": 5, "mode": "standard"},
                ),
            )
        self.client = client

    @staticmethod
    def _key(key: str) -> str:
        clean = key.replace("\\", "/").lstrip("/")
        if not clean or ".." in clean.split("/"):
            raise ValueError("invalid blob key")
        return clean

    def put(self, key: str, data: bytes | BinaryIO, *, content_type: str = "application/octet-stream") -> str:
        k = self._key(key)
        if isinstance(data, (bytes, bytearray)):
            self.client.put_object(Bucket=self.bucket, Key=k, Body=bytes(data), ContentType=content_type)
        else:
            # Streams + multipart for large exhibit bundles.
            self.client.upload_fileobj(data, self.bucket, k, ExtraArgs={"ContentType": content_type})
        return key

    def _is_missing(self, exc: Exception) -> bool:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound"}

    def get(self, key: str) -> bytes:
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if self._is_missing(exc):
                raise FileNotFoundError(key) from exc
            raise
        return resp["Body"].read()

    def get_file(self, key: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, self._key(key), str(dest))
        except Exception as exc:
            if self._is_missing(exc):
                raise FileNotFoundError(key) from exc
            raise
        return dest

    def delete(self, key: str) -> bool:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if self._is_missing(exc):
                return True
            logger.warning("s3 delete failed key=%s", key, exc_info=True)
            return False
        return True

    def delete_prefix(self, prefix: str) -> int:
        p = self._key(prefix.rstrip("/")) + "/"
        removed = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=p):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", []) or []]
            if not objs:
                continue
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objs, "Quiet": True})
            removed += len(objs)
        return removed

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as exc:
            if self._is_missing(exc):
                return False
            raise

    def signed_upload_url(self, key: str, *, content_type: str, expires_s: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": self._key(key), "ContentType": content_type},
            ExpiresIn=max(60, int(expires_s)),
        )

    def signed_download_url(self, key: str, *, expires_s: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key)},
            ExpiresIn=max(60, int(expires_s)),
        )


_store: BlobStore | None = None


def get_blob_store() -> BlobStore:
    global _store
    if _store is not None:
        return _store
    backend = (settings.blob_backend or "local").strip().lower()
    if backend in {"s3", "r2"}:
        _store = S3BlobStore()
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


def job_cancel_key(job_id: str) -> str:
    """Worker-visible cancel marker (written by analysis cancel endpoint)."""
    return f"{job_bundle_prefix(job_id)}/cancel.flag"
