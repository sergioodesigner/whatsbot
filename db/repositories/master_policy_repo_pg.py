"""Supabase Postgres repository for master policy / global config (Phase 2).

Drop-in equivalent of ``db.repositories.master_policy_repo``.
"""

from __future__ import annotations

import json

from db import master_pg_connection as pg


def get_global(key: str, default=None):
    row = pg.fetchone("SELECT value FROM global_config WHERE key = %s", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_global(key: str, value) -> None:
    pg.execute(
        """
        INSERT INTO global_config (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_tenant(tenant_slug: str, key: str, default=None):
    row = pg.fetchone(
        "SELECT value FROM tenant_policies WHERE tenant_slug = %s AND key = %s",
        (tenant_slug, key),
    )
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_tenant(tenant_slug: str, key: str, value) -> None:
    pg.execute(
        """
        INSERT INTO tenant_policies (tenant_slug, key, value) VALUES (%s, %s, %s)
        ON CONFLICT (tenant_slug, key) DO UPDATE SET value = EXCLUDED.value
        """,
        (tenant_slug, key, json.dumps(value, ensure_ascii=False)),
    )
