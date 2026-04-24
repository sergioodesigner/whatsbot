"""Supabase Postgres repository for config key-value storage (Phase 4).

Drop-in equivalent of ``db.repositories.config_repo`` backed by Postgres.
"""

from __future__ import annotations

import json

from db import tenant_pg_connection as pg


def get_all() -> dict:
    """Return all config key-value pairs as a dict (values JSON-decoded)."""
    slug = pg._get_slug()
    rows = pg.fetchall("SELECT key, value FROM config WHERE tenant_slug = %s", (slug,))
    result = {}
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            result[row["key"]] = row["value"]
    return result


def get(key: str, default=None):
    """Get a single config value by key."""
    slug = pg._get_slug()
    row = pg.fetchone("SELECT value FROM config WHERE key = %s AND tenant_slug = %s", (key, slug))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set(key: str, value) -> None:
    """Set a single config value (JSON-encoded)."""
    slug = pg._get_slug()
    pg.execute(
        """
        INSERT INTO config (tenant_slug, key, value) 
        VALUES (%s, %s, %s)
        ON CONFLICT (tenant_slug, key) DO UPDATE SET value = EXCLUDED.value
        """,
        (slug, key, json.dumps(value, ensure_ascii=False)),
    )


def set_many(data: dict) -> None:
    """Set multiple config values at once."""
    slug = pg._get_slug()
    with pg.get_pg_conn() as conn:
        with conn.cursor() as cur:
            for k, v in data.items():
                cur.execute(
                    """
                    INSERT INTO config (tenant_slug, key, value) 
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_slug, key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (slug, k, json.dumps(v, ensure_ascii=False)),
                )
        conn.commit()
