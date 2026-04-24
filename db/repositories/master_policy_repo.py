"""Repository for global and per-tenant policy flags in master DB."""

import json

from db.master_connection import get_master_db


def get_global(key: str, default=None):
    conn = get_master_db()
    row = conn.execute("SELECT value FROM global_config WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_global(key: str, value) -> None:
    conn = get_master_db()
    conn.execute(
        "INSERT OR REPLACE INTO global_config (key, value) VALUES (?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


def get_tenant(tenant_slug: str, key: str, default=None):
    conn = get_master_db()
    row = conn.execute(
        "SELECT value FROM tenant_policies WHERE tenant_slug = ? AND key = ?",
        (tenant_slug, key),
    ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        return row["value"]


def set_tenant(tenant_slug: str, key: str, value) -> None:
    conn = get_master_db()
    conn.execute(
        "INSERT OR REPLACE INTO tenant_policies (tenant_slug, key, value) VALUES (?, ?, ?)",
        (tenant_slug, key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()
