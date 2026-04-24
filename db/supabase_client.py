"""Supabase client singleton for WhatsBot.

Initialised lazily on first access.  Requires the following env vars:
  - SUPABASE_URL
  - SUPABASE_SERVICE_ROLE_KEY   (backend only – never expose to frontend)

Optional:
  - SUPABASE_DB_URL             (postgres:// DSN for direct psycopg2 access)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_client = None          # supabase.Client
_db_url: str | None = None


def get_url() -> str:
    url = os.environ.get("SUPABASE_URL", "").strip()
    if not url:
        raise RuntimeError("SUPABASE_URL env var is not set.")
    return url


def get_service_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY env var is not set.")
    return key


def get_client():
    """Return the shared Supabase client (lazy init)."""
    global _client
    if _client is None:
        try:
            from supabase import create_client  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "supabase-py is not installed. Add 'supabase' to requirements.txt."
            ) from exc
        _client = create_client(get_url(), get_service_key())
        logger.info("Supabase client initialised (url=%s)", get_url())
    return _client


def get_db_url() -> str | None:
    """Return the Postgres DSN for direct connections (Phase 2+)."""
    return os.environ.get("SUPABASE_DB_URL", "").strip() or None


def is_configured() -> bool:
    """Return True if Supabase env vars are present (regardless of SDK)."""
    return bool(
        os.environ.get("SUPABASE_URL", "").strip()
        and os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )
