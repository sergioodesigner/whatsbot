"""Supabase Postgres connection for tenant data (Phase 3 & 4).

Reuses the underlying master Postgres connection but provides a tenant-scoped
API so that all queries automatically filter by ``tenant_slug``.

Feature flag: ``CRM_AUTOMATION_BACKEND=sqlite|supabase``
"""

from __future__ import annotations

import logging
import os

from contextlib import contextmanager

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

-- Phase 4: Core Conversational Tables

CREATE TABLE IF NOT EXISTS config (
    tenant_slug TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (tenant_slug, key)
);

CREATE TABLE IF NOT EXISTS contacts (
    id              SERIAL PRIMARY KEY,
    tenant_slug     TEXT    NOT NULL,
    phone           TEXT    NOT NULL,
    name            TEXT    NOT NULL DEFAULT '',
    email           TEXT    NOT NULL DEFAULT '',
    profession      TEXT    NOT NULL DEFAULT '',
    company         TEXT    NOT NULL DEFAULT '',
    address         TEXT    NOT NULL DEFAULT '',
    ai_enabled      INTEGER NOT NULL DEFAULT 1,
    is_group        INTEGER NOT NULL DEFAULT 0,
    group_name      TEXT    NOT NULL DEFAULT '',
    is_archived     INTEGER NOT NULL DEFAULT 0,
    archived_by_app INTEGER NOT NULL DEFAULT 0,
    can_send        INTEGER NOT NULL DEFAULT 1,
    unread_count    INTEGER NOT NULL DEFAULT 0,
    unread_ai_count INTEGER NOT NULL DEFAULT 0,
    created_at      DOUBLE PRECISION NOT NULL,
    updated_at      DOUBLE PRECISION NOT NULL,
    UNIQUE(tenant_slug, phone)
);
CREATE INDEX IF NOT EXISTS idx_contacts_tenant_updated ON contacts(tenant_slug, updated_at);
CREATE INDEX IF NOT EXISTS idx_contacts_tenant_archived ON contacts(tenant_slug, is_archived);

CREATE TABLE IF NOT EXISTS observations (
    id         SERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    text       TEXT    NOT NULL,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_tenant_contact ON observations(tenant_slug, contact_id);

CREATE TABLE IF NOT EXISTS messages (
    id         SERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    ts         DOUBLE PRECISION NOT NULL,
    media_type TEXT,
    media_path TEXT,
    status     TEXT,
    msg_id     TEXT
);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_path TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS msg_id TEXT;
CREATE INDEX IF NOT EXISTS idx_msg_tenant_contact_ts ON messages(tenant_slug, contact_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_tenant_id ON messages(tenant_slug, msg_id);

CREATE TABLE IF NOT EXISTS usage (
    id                SERIAL PRIMARY KEY,
    tenant_slug       TEXT NOT NULL,
    contact_id        INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    call_type         TEXT    NOT NULL,
    model             TEXT    NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    cost_usd          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ts                DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_contact_ts ON usage(tenant_slug, contact_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_ts ON usage(tenant_slug, ts);

CREATE TABLE IF NOT EXISTS tags (
    id    SERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    name  TEXT    NOT NULL,
    color TEXT    NOT NULL,
    UNIQUE(tenant_slug, name)
);

CREATE TABLE IF NOT EXISTS contact_tags (
    tenant_slug TEXT NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_slug, contact_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_ct_tenant_tag ON contact_tags(tenant_slug, tag_id);

CREATE TABLE IF NOT EXISTS unread_msg_ids (
    id         SERIAL PRIMARY KEY,
    tenant_slug TEXT NOT NULL,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    msg_id     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unread_tenant_contact ON unread_msg_ids(tenant_slug, contact_id);

CREATE TABLE IF NOT EXISTS executions (
    id           SERIAL PRIMARY KEY,
    tenant_slug  TEXT NOT NULL,
    phone        TEXT    NOT NULL,
    trigger_type TEXT    NOT NULL DEFAULT 'webhook',
    status       TEXT    NOT NULL DEFAULT 'running',
    started_at   DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_tenant_started ON executions(tenant_slug, started_at);

CREATE TABLE IF NOT EXISTS execution_steps (
    id           SERIAL PRIMARY KEY,
    tenant_slug  TEXT NOT NULL,
    execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    step_type    TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'ok',
    data         TEXT,
    ts           DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step_tenant_exec ON execution_steps(tenant_slug, execution_id);
"""

def is_core_supabase_backend() -> bool:
    """Return True when Phase 4 is active via Supabase Postgres."""
    if os.environ.get("MASTER_DB_BACKEND", "sqlite").strip().lower() != "supabase":
        return False
    return os.environ.get("CORE_DB_BACKEND", "sqlite").strip().lower() == "supabase"

def is_crm_supabase_backend() -> bool:
    """Return True when Phase 3 is active via Supabase Postgres."""
    # Depends on Phase 2 being active first
    if os.environ.get("MASTER_DB_BACKEND", "sqlite").strip().lower() != "supabase":
        return False
    return os.environ.get("CRM_AUTOMATION_BACKEND", "sqlite").strip().lower() == "supabase"


def init_tenant_pg_schema() -> None:
    """Apply the tenant schema if Phase 3 or Phase 4 is active.
    This runs on startup alongside Phase 2 initialization.
    """
    if not (is_crm_supabase_backend() or is_core_supabase_backend()):
        return
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_TENANT_SCHEMA_PG)
            # Ensure new columns exist for installations that ran setup scripts before these were added
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_type TEXT;")
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_path TEXT;")
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS status TEXT;")
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS msg_id TEXT;")
        conn.commit()
    logger.info("Supabase tenant schema initialised (Phases 3/4).")


# ── Tenant-scoped API ──────────────────────────────────────────────────

def _get_slug() -> str:
    slug = current_tenant_slug.get()
    if not slug or slug == "default":
        # In single-tenant mode, fallback to a default tenant namespace
        return "single_tenant_default"
    return slug


@contextmanager
def dict_cursor():
    """Context manager that yields (conn, cursor) with RealDictCursor.

    Use this whenever a repository needs a raw cursor so that rows are always
    returned as dicts (consistent with fetchone/fetchall helpers).
    """
    import psycopg2.extras
    with get_pg_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield conn, cur
        finally:
            cur.close()

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
