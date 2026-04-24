"""Supabase Postgres connection for the master database (Phase 2).

Uses psycopg2 (synchronous) via the direct Postgres DSN provided by
``SUPABASE_DB_URL``.  The connection is pooled with a simple thread-local
strategy matching the existing SQLite pattern.

Feature flag: ``MASTER_DB_BACKEND=sqlite|supabase``
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_local = threading.local()
_db_url: str | None = None

# ── Schema DDL (run once at startup) ─────────────────────────────────

_MASTER_SCHEMA_PG = """
-- Supabase Postgres master schema (idempotent)

CREATE TABLE IF NOT EXISTS tenants (
    id              SERIAL PRIMARY KEY,
    slug            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    custom_domain   TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'active',
    plan            TEXT    NOT NULL DEFAULT 'free',
    gowa_port       INTEGER NOT NULL UNIQUE,
    max_contacts    INTEGER NOT NULL DEFAULT 500,
    openrouter_api_key TEXT NOT NULL DEFAULT '',
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug   ON tenants(slug);
CREATE INDEX IF NOT EXISTS idx_tenants_status ON tenants(status);

CREATE TABLE IF NOT EXISTS superadmins (
    id            SERIAL PRIMARY KEY,
    username      TEXT   NOT NULL UNIQUE,
    password_hash TEXT   NOT NULL,
    salt          TEXT   NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS global_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_policies (
    tenant_slug TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    PRIMARY KEY (tenant_slug, key)
);

CREATE TABLE IF NOT EXISTS tenant_company_profile (
    tenant_slug         TEXT PRIMARY KEY,
    owner_name          TEXT NOT NULL DEFAULT '',
    owner_phone         TEXT NOT NULL DEFAULT '',
    plan_name           TEXT NOT NULL DEFAULT '',
    plan_amount         DOUBLE PRECISION NOT NULL DEFAULT 0,
    due_day             INTEGER NOT NULL DEFAULT 10,
    contract_start_ts   DOUBLE PRECISION,
    contract_end_ts     DOUBLE PRECISION,
    notes               TEXT NOT NULL DEFAULT '',
    updated_at          DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tenant_billing_invoices (
    id          SERIAL PRIMARY KEY,
    tenant_slug TEXT   NOT NULL,
    period_ym   TEXT   NOT NULL,
    due_ts      DOUBLE PRECISION NOT NULL,
    amount      DOUBLE PRECISION NOT NULL DEFAULT 0,
    paid        INTEGER NOT NULL DEFAULT 0,
    paid_at     DOUBLE PRECISION,
    notes       TEXT NOT NULL DEFAULT '',
    UNIQUE(tenant_slug, period_ym)
);

CREATE INDEX IF NOT EXISTS idx_tenant_billing_tenant ON tenant_billing_invoices(tenant_slug);
CREATE INDEX IF NOT EXISTS idx_tenant_billing_due    ON tenant_billing_invoices(due_ts);
"""


# ── Backend check ─────────────────────────────────────────────────────

def is_supabase_backend() -> bool:
    """Return True when the master DB is configured to use Supabase Postgres."""
    return os.environ.get("MASTER_DB_BACKEND", "sqlite").strip().lower() == "supabase"


# ── Connection management ─────────────────────────────────────────────

def init_master_pg(db_url: str) -> None:
    """Initialise the Postgres master database.

    Creates tables if they don't exist.  Call once at startup.
    """
    global _db_url
    _db_url = db_url
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_MASTER_SCHEMA_PG)
        conn.commit()
    logger.info("Supabase master DB initialised (pg).")


def _get_conn():
    """Return (or open) a thread-local psycopg2 connection."""
    global _db_url
    if _db_url is None:
        raise RuntimeError(
            "Supabase master DB not initialised. Call init_master_pg() first."
        )
    conn = getattr(_local, "conn", None)
    if conn is None or conn.closed:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:
            raise RuntimeError(
                "psycopg2 is not installed. Add 'psycopg2-binary' to requirements.txt."
            ) from exc
        conn = psycopg2.connect(_db_url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        _local.conn = conn
    return conn


@contextmanager
def get_pg_conn():
    """Context manager yielding a psycopg2 connection."""
    conn = _get_conn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


def fetchone(sql: str, params: tuple = ()) -> dict | None:
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def fetchall(sql: str, params: tuple = ()) -> list[dict]:
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def execute(sql: str, params: tuple = (), *, commit: bool = True) -> int:
    """Execute a DML statement and return rowcount."""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rowcount = cur.rowcount
        if commit:
            conn.commit()
        return rowcount


def execute_returning(sql: str, params: tuple = ()) -> dict | None:
    """Execute INSERT/UPDATE … RETURNING and return the first row."""
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
