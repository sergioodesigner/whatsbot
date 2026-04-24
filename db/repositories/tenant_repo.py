"""Repository for tenant CRUD operations on the master database."""

import time

from db.master_connection import get_master_db

# First GOWA port to assign to tenants (increments from here)
_BASE_GOWA_PORT = 65001


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return {}
    return dict(row)


def list_all(*, status: str | None = None) -> list[dict]:
    """Return all tenants, optionally filtered by status."""
    conn = get_master_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM tenants WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tenants ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_by_slug(slug: str) -> dict:
    """Return a tenant by its subdomain slug."""
    conn = get_master_db()
    row = conn.execute(
        "SELECT * FROM tenants WHERE slug = ?", (slug,)
    ).fetchone()
    return _row_to_dict(row)


def get_by_id(tenant_id: int) -> dict:
    """Return a tenant by its ID."""
    conn = get_master_db()
    row = conn.execute(
        "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    return _row_to_dict(row)


def get_by_custom_domain(domain: str) -> dict:
    """Return a tenant by its custom domain."""
    conn = get_master_db()
    row = conn.execute(
        "SELECT * FROM tenants WHERE custom_domain = ?", (domain,)
    ).fetchone()
    return _row_to_dict(row)


def _next_gowa_port() -> int:
    """Return the next available GOWA port."""
    conn = get_master_db()
    row = conn.execute("SELECT MAX(gowa_port) as max_port FROM tenants").fetchone()
    if row and row["max_port"]:
        return row["max_port"] + 1
    return _BASE_GOWA_PORT


def create(slug: str, name: str, **kwargs) -> dict:
    """Create a new tenant and return its data.

    Optional kwargs: custom_domain, status, plan, max_contacts, openrouter_api_key.
    """
    conn = get_master_db()
    now = time.time()
    gowa_port = kwargs.pop("gowa_port", None) or _next_gowa_port()

    conn.execute(
        """INSERT INTO tenants (slug, name, custom_domain, status, plan,
           gowa_port, max_contacts, openrouter_api_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    conn.commit()
    return get_by_slug(slug)


def update(slug: str, **kwargs) -> dict:
    """Update tenant fields. Only provided kwargs are updated."""
    conn = get_master_db()
    allowed = {"name", "custom_domain", "status", "plan", "max_contacts", "openrouter_api_key"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_by_slug(slug)

    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [slug]

    conn.execute(
        f"UPDATE tenants SET {set_clause} WHERE slug = ?",
        values,
    )
    conn.commit()
    return get_by_slug(slug)


def set_status(slug: str, status: str) -> dict:
    """Change a tenant's status (active, suspended, trial)."""
    return update(slug, status=status)


def delete(slug: str) -> bool:
    """Delete a tenant record (does NOT delete the tenant's database files)."""
    conn = get_master_db()
    cursor = conn.execute("DELETE FROM tenants WHERE slug = ?", (slug,))
    conn.commit()
    return cursor.rowcount > 0


def count() -> int:
    """Return total number of tenants."""
    conn = get_master_db()
    row = conn.execute("SELECT COUNT(*) as c FROM tenants").fetchone()
    return row["c"] if row else 0


def count_active() -> int:
    """Return number of active tenants."""
    conn = get_master_db()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM tenants WHERE status = 'active'"
    ).fetchone()
    return row["c"] if row else 0


# ── Superadmin ────────────────────────────────────────────────────────

def get_superadmin(username: str) -> dict:
    """Return a superadmin by username."""
    conn = get_master_db()
    row = conn.execute(
        "SELECT * FROM superadmins WHERE username = ?", (username,)
    ).fetchone()
    return _row_to_dict(row)


def create_superadmin(username: str, password_hash: str, salt: str) -> dict:
    """Create a superadmin account."""
    conn = get_master_db()
    now = time.time()
    conn.execute(
        "INSERT INTO superadmins (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
        (username, password_hash, salt, now),
    )
    conn.commit()
    return get_superadmin(username)


def superadmin_exists() -> bool:
    """Check if at least one superadmin account exists."""
    conn = get_master_db()
    row = conn.execute("SELECT COUNT(*) as c FROM superadmins").fetchone()
    return (row["c"] if row else 0) > 0


def list_superadmins() -> list[dict]:
    """Return all superadmin accounts (for token validation)."""
    conn = get_master_db()
    rows = conn.execute(
        "SELECT * FROM superadmins ORDER BY created_at ASC"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]
