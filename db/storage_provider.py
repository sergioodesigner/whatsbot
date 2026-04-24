"""Storage provider abstraction for WhatsBot.

Supports two backends controlled by the ``STORAGE_BACKEND`` env var:
  - ``local``    – save files to the local filesystem (current default).
  - ``supabase`` – upload files to Supabase Storage.

Usage
-----
    from db.storage_provider import get_provider

    provider = get_provider()
    url = provider.upload("media", filename, content_bytes, content_type="image/jpeg")
    url = provider.public_url("media", filename)
    provider.delete("media", filename)

Feature flags
-------------
  STORAGE_BACKEND=local|supabase
  STORAGE_WRITE_THROUGH=true|false   # write to both (useful for cutover)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Feature flags ─────────────────────────────────────────────────────

def _backend() -> str:
    return os.environ.get("STORAGE_BACKEND", "local").strip().lower()

def _write_through() -> bool:
    return os.environ.get("STORAGE_WRITE_THROUGH", "false").strip().lower() in ("1", "true", "yes")


# ── Provider interface ────────────────────────────────────────────────

class StorageProvider:
    """Abstract storage provider."""

    def upload(
        self,
        bucket: str,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload data and return a URL/path usable by the app.

        Returns an absolute URL (remote) or a relative path (local).
        """
        raise NotImplementedError

    def public_url(self, bucket: str, object_key: str) -> str:
        """Return the public/signed URL for an object."""
        raise NotImplementedError

    def delete(self, bucket: str, object_key: str) -> None:
        """Delete an object (best-effort, no exception on missing)."""
        raise NotImplementedError


# ── Local filesystem provider ─────────────────────────────────────────

class LocalStorageProvider(StorageProvider):
    """Stores files in the local ``statics/`` directory tree."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def _resolve(self, bucket: str, object_key: str) -> Path:
        path = self.data_dir / "statics" / bucket / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def upload(self, bucket: str, object_key: str, data: bytes,
               content_type: str = "application/octet-stream") -> str:
        dest = self._resolve(bucket, object_key)
        dest.write_bytes(data)
        return f"statics/{bucket}/{object_key}"

    def public_url(self, bucket: str, object_key: str) -> str:
        return f"statics/{bucket}/{object_key}"

    def delete(self, bucket: str, object_key: str) -> None:
        dest = self._resolve(bucket, object_key)
        try:
            dest.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("[LocalStorage] delete failed: %s", exc)


# ── Supabase Storage provider ─────────────────────────────────────────

class SupabaseStorageProvider(StorageProvider):
    """Stores files in Supabase Storage buckets.

    Bucket names must exist in Supabase (create them via the dashboard or
    the migration script).  Recommended buckets:
      - ``media``      – incoming and outgoing media files
      - ``avatars``    – WhatsApp profile photos
      - ``senditems``  – operator-sent attachments
    """

    def __init__(self, local_fallback: LocalStorageProvider | None = None):
        self._local = local_fallback

    def _storage(self):
        from db.supabase_client import get_client
        return get_client().storage

    def upload(self, bucket: str, object_key: str, data: bytes,
               content_type: str = "application/octet-stream") -> str:
        try:
            storage = self._storage()
            # upsert=True avoids 409 on duplicate keys
            storage.from_(bucket).upload(
                path=object_key,
                file=data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            url = storage.from_(bucket).get_public_url(object_key)
            logger.debug("[SupabaseStorage] Uploaded %s/%s → %s", bucket, object_key, url)
            return url
        except Exception as exc:
            logger.error(
                "[SupabaseStorage] upload failed for %s/%s: %s – falling back to local",
                bucket, object_key, exc,
            )
            if self._local:
                return self._local.upload(bucket, object_key, data, content_type)
            raise

    def public_url(self, bucket: str, object_key: str) -> str:
        try:
            return self._storage().from_(bucket).get_public_url(object_key)
        except Exception as exc:
            logger.warning("[SupabaseStorage] public_url failed: %s", exc)
            if self._local:
                return self._local.public_url(bucket, object_key)
            return ""

    def delete(self, bucket: str, object_key: str) -> None:
        try:
            self._storage().from_(bucket).remove([object_key])
        except Exception as exc:
            logger.warning("[SupabaseStorage] delete failed: %s", exc)


# ── Write-through provider (transition helper) ────────────────────────

class WriteThroughProvider(StorageProvider):
    """Writes to both local and remote, reads from remote (cutover helper)."""

    def __init__(self, primary: StorageProvider, secondary: StorageProvider):
        self._primary = primary
        self._secondary = secondary

    def upload(self, bucket: str, object_key: str, data: bytes,
               content_type: str = "application/octet-stream") -> str:
        result = self._primary.upload(bucket, object_key, data, content_type)
        try:
            self._secondary.upload(bucket, object_key, data, content_type)
        except Exception as exc:
            logger.warning("[WriteThroughStorage] secondary upload failed: %s", exc)
        return result

    def public_url(self, bucket: str, object_key: str) -> str:
        return self._primary.public_url(bucket, object_key)

    def delete(self, bucket: str, object_key: str) -> None:
        self._primary.delete(bucket, object_key)
        try:
            self._secondary.delete(bucket, object_key)
        except Exception:
            pass


# ── Factory ───────────────────────────────────────────────────────────

_provider: StorageProvider | None = None


def init_provider(data_dir: Path) -> StorageProvider:
    """Initialise (or re-initialise) the global storage provider.

    Call once at application startup from ``main.py`` after env vars load.
    """
    global _provider
    backend = _backend()
    local = LocalStorageProvider(data_dir)

    if backend == "supabase":
        from db.supabase_client import is_configured
        if not is_configured():
            logger.warning(
                "[Storage] STORAGE_BACKEND=supabase but Supabase env vars are missing. "
                "Falling back to local storage."
            )
            _provider = local
        else:
            supabase = SupabaseStorageProvider(local_fallback=local)
            if _write_through():
                logger.info("[Storage] Write-through mode active (local + supabase).")
                _provider = WriteThroughProvider(supabase, local)
            else:
                logger.info("[Storage] Backend: supabase")
                _provider = supabase
    else:
        logger.info("[Storage] Backend: local")
        _provider = local

    return _provider


def get_provider() -> StorageProvider:
    """Return the active storage provider (must call ``init_provider`` first)."""
    if _provider is None:
        raise RuntimeError(
            "Storage provider not initialised. Call db.storage_provider.init_provider() first."
        )
    return _provider
