"""Supabase Postgres repository for tenant CRUD (Phase 2 master migration).

Drop-in equivalent of ``db.repositories.tenant_repo`` backed by Postgres
instead of SQLite.

All public functions have the same signature as the SQLite version.
"""

from __future__ import annotations

import time

from db import master_pg_connection as pg

_BASE_GOWA_PORT = 65001


# ── Helpers ───────────────────────────────────────────────────────────

def _next_gowa_port() -> int:
    row = pg.fetchone("SELECT MAX(gowa_port) AS max_port FROM tenants")
    if row and row.get("max_port"):
        return row["max_port"] + 1
    return _BASE_GOWA_PORT


# ── Tenants ───────────────────────────────────────────────────────────

def list_all(*, status: str | None = None) -> list[dict]:
    if status:
        return pg.fetchall(
            "SELECT * FROM tenants WHERE status = %s ORDER BY created_at DESC",
            (status,),
        )
    return pg.fetchall("SELECT * FROM tenants ORDER BY created_at DESC")


def get_by_slug(slug: str) -> dict:
    row = pg.fetchone("SELECT * FROM tenants WHERE slug = %s", (slug,))
    return row or {}


def get_by_id(tenant_id: int) -> dict:
    row = pg.fetchone("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
    return row or {}


def get_by_custom_domain(domain: str) -> dict:
    row = pg.fetchone("SELECT * FROM tenants WHERE custom_domain = %s", (domain,))
    return row or {}


def create(slug: str, name: str, **kwargs) -> dict:
    now = time.time()
    gowa_port = kwargs.pop("gowa_port", None) or _next_gowa_port()
    row = pg.execute_returning(
        """
        INSERT INTO tenants
            (slug, name, custom_domain, status, plan, gowa_port, max_contacts,
             openrouter_api_key, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            slug,
            name,
            kwargs.get("custom_domain", ""),
            kwargs.get("status", "active"),
            kwargs.get("plan", "free"),
            gowa_port,
            kwargs.get("max_contacts", 500),
            kwargs.get("openrouter_api_key", ""),
            now,
            now,
        ),
    )
    return row or {}


def update(slug: str, **kwargs) -> dict:
    allowed = {"name", "custom_domain", "status", "plan", "max_contacts", "openrouter_api_key"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_by_slug(slug)
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [slug]
    pg.execute(f"UPDATE tenants SET {set_clause} WHERE slug = %s", tuple(values))
    return get_by_slug(slug)


def set_status(slug: str, status: str) -> dict:
    return update(slug, status=status)


def delete(slug: str) -> bool:
    rows = pg.execute("DELETE FROM tenants WHERE slug = %s", (slug,))
    return rows > 0


def count() -> int:
    row = pg.fetchone("SELECT COUNT(*) AS c FROM tenants")
    return row["c"] if row else 0


def count_active() -> int:
    row = pg.fetchone("SELECT COUNT(*) AS c FROM tenants WHERE status = 'active'")
    return row["c"] if row else 0


# ── Superadmins ───────────────────────────────────────────────────────

def get_superadmin(username: str) -> dict:
    row = pg.fetchone("SELECT * FROM superadmins WHERE username = %s", (username,))
    return row or {}


def create_superadmin(username: str, password_hash: str, salt: str) -> dict:
    now = time.time()
    row = pg.execute_returning(
        """
        INSERT INTO superadmins (username, password_hash, salt, created_at)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (username, password_hash, salt, now),
    )
    return row or {}


def superadmin_exists() -> bool:
    row = pg.fetchone("SELECT COUNT(*) AS c FROM superadmins")
    return (row["c"] if row else 0) > 0


def list_superadmins() -> list[dict]:
    return pg.fetchall("SELECT * FROM superadmins ORDER BY created_at ASC")
