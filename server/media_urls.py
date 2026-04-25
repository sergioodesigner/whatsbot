"""Transform Supabase Storage public URLs into time-limited signed URLs.

Private buckets return 403 for ``/object/public/...`` in the browser; signing
fixes images, GIFs, video and audio without storing signed URLs in the DB.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import unquote

logger = logging.getLogger(__name__)

_PUBLIC_OBJECT_RE = re.compile(
    r"^https?://[^/]+\.supabase\.(?:co|in)/storage/v1/object/public/([^/]+)/(.+)$",
    re.IGNORECASE,
)


def sign_supabase_public_media_url(url: str | None, expires_in: int | None = None) -> str | None:
    """If *url* is a Supabase public-object URL, return a signed download URL."""
    if not url or not isinstance(url, str):
        return url
    clean = url.strip()
    if not clean or "/object/sign/" in clean:
        return url
    m = _PUBLIC_OBJECT_RE.match(clean.split("?")[0])
    if not m:
        return url
    bucket = m.group(1)
    object_key = unquote(m.group(2).split("?")[0]).strip()
    if not object_key:
        return url
    if expires_in is None:
        try:
            expires_in = int(os.environ.get("SUPABASE_MEDIA_SIGN_EXPIRES", "604800"))
        except ValueError:
            expires_in = 604800
    expires_in = max(60, min(int(expires_in), 60 * 60 * 24 * 365))
    try:
        from db.supabase_client import get_client

        res = get_client().storage.from_(bucket).create_signed_url(object_key, expires_in)
        if isinstance(res, dict):
            signed = res.get("signedURL") or res.get("signedUrl")
            if signed:
                return str(signed)
    except Exception as exc:
        logger.warning(
            "[MediaURL] create_signed_url failed bucket=%r key=%r: %s",
            bucket,
            object_key[:120],
            exc,
        )
    return url


def enrich_message_media_path(msg: dict) -> None:
    """Mutate *msg* in place: replace ``media_path`` with a signed URL when applicable."""
    if not msg:
        return
    mt = msg.get("media_type")
    mp = msg.get("media_path")
    if not mp or mt not in ("image", "video", "gif", "audio"):
        return
    signed = sign_supabase_public_media_url(mp)
    if signed:
        msg["media_path"] = signed
