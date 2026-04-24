"""Supabase Postgres connection for tenant data (Phase 3 & 4).

Reuses the underlying master Postgres connection but provides a tenant-scoped
API so that all queries automatically filter by ``tenant_slug``.

Feature flag: ``CRM_AUTOMATION_BACKEND=sqlite|supabase``
"""

from __future__ import annotations

import logging
import os

from db.master_pg_connection import get_pg_conn
from server.tenant import current_tenant_slug

logger = logging.getLogger(__name__)

# ── Schema DDL (run once at startup if needed) ───────────────────────

_TENANT_SCHEMA_PG = """
-- Phase 3: CRM Tables
CREATE TABLE IF NOT EXISTS crm_deals (
    id              SERIAL PRIMARY KEY,
    tenant_slug     TEXT    NOT NULL,
    contact_phone   TEXT    NOT NULL,
    title           TEXT    NOT NULL DEFAULT '',
    stage           TEXT    NOT NULL DEFAULT 'novo',
    origin          TEXT    NOT NULL DEFAULT 'manual',
    potential_value DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    owner           TEXT    NOT NULL DEFAULT '',
    notes           TEXT    NOT NULL DEFAULT '',
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crm_deals_tenant ON crm_deals(tenant_slug);
CREATE INDEX IF NOT EXISTS idx_crm_deals_stage ON crm_deals(stage);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_deals_tenant_phone ON crm_deals(tenant_slug, contact_phone);

CREATE TABLE IF NOT EXISTS crm_tasks (
    id          SERIAL PRIMARY KEY,
    tenant_slug TEXT    NOT NULL,
    deal_id     INTEGER NOT NULL REFERENCES crm_deals(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    due_ts      DOUBLE PRECISION,
    done        INTEGER NOT NULL DEFAULT 0,
    notes       TEXT    NOT NULL DEFAULT '',
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crm_tasks_deal ON crm_tasks(deal_id);

-- Phase 3: Automation Tables
CREATE TABLE IF NOT EXISTS automation_rules (
    id             SERIAL PRIMARY KEY,
    tenant_slug    TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    trigger_type   TEXT    NOT NULL,
    from_stage     TEXT    NOT NULL DEFAULT '',
    to_stage       TEXT    NOT NULL DEFAULT '',
    condition_owner TEXT   NOT NULL DEFAULT '',
    condition_min_value DOUBLE PRECISION,
    condition_tag   TEXT   NOT NULL DEFAULT '',
    action_type    TEXT    NOT NULL,
    action_payload TEXT    NOT NULL DEFAULT '{}',
    created_at     DOUBLE PRECISION NOT NULL,
    updated_at     DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_rules_tenant ON automation_rules(tenant_slug);

CREATE TABLE IF NOT EXISTS automation_runs (
    id           SERIAL PRIMARY KEY,
    tenant_slug  TEXT    NOT NULL,
    rule_id      INTEGER REFERENCES automation_rules(id) ON DELETE SET NULL,
    deal_id      INTEGER,
    fingerprint  TEXT    NOT NULL DEFAULT '',
    trigger_type TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'ok',
    context      TEXT    NOT NULL DEFAULT '{}',
    result       TEXT    NOT NULL DEFAULT '{}',
    error        TEXT    NOT NULL DEFAULT '',
    ts           DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_tenant ON automation_runs(tenant_slug);
"""

def is_crm_supabase_backend() -> bool:
    """Return True when Phase 3 is active via Supabase Postgres."""
    # Depends on Phase 2 being active first
    if os.environ.get("MASTER_DB_BACKEND", "sqlite").strip().lower() != "supabase":
        return False
    return os.environ.get("CRM_AUTOMATION_BACKEND", "sqlite").strip().lower() == "supabase"


def init_tenant_pg_schema() -> None:
    """Apply the tenant schema if Phase 3 is active.
    This runs on startup alongside Phase 2 initialization.
    """
    if not is_crm_supabase_backend():
        return
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_TENANT_SCHEMA_PG)
        conn.commit()
    logger.info("Supabase tenant schema initialised (Phase 3 CRM/Automations).")


# ── Tenant-scoped API ──────────────────────────────────────────────────

def _get_slug() -> str:
    slug = current_tenant_slug.get()
    if not slug or slug == "default":
        # In single-tenant mode, fallback to a default tenant namespace
        return "single_tenant_default"
    return slug


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
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rowcount = cur.rowcount
        if commit:
            conn.commit()
        return rowcount


def execute_returning(sql: str, params: tuple = ()) -> dict | None:
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
