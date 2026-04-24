"""Supabase Postgres repository for tags and contact_tags tables (Phase 4).

Drop-in equivalent of ``db.repositories.tag_repo`` backed by Postgres.
"""

from __future__ import annotations

from db import tenant_pg_connection as pg


def get_all() -> dict[str, dict]:
    """Return all tags as {name: {color: ...}} dict."""
    slug = pg._get_slug()
    rows = pg.fetchall("SELECT name, color FROM tags WHERE tenant_slug = %s ORDER BY name", (slug,))
    return {r["name"]: {"color": r["color"]} for r in rows}


def get_by_name(name: str) -> dict | None:
    """Get a tag by name. Returns {id, name, color} or None."""
    slug = pg._get_slug()
    row = pg.fetchone("SELECT * FROM tags WHERE name = %s AND tenant_slug = %s", (name, slug))
    return dict(row) if row else None


def create(name: str, color: str) -> bool:
    """Create a tag. Returns False if name already exists."""
    slug = pg._get_slug()
    existing = pg.fetchone("SELECT 1 FROM tags WHERE name = %s AND tenant_slug = %s", (name, slug))
    if existing:
        return False
    pg.execute("INSERT INTO tags (tenant_slug, name, color) VALUES (%s, %s, %s)", (slug, name, color))
    return True


def update(old_name: str, *, new_name: str | None = None, color: str | None = None) -> bool:
    """Update a tag's name and/or color. Returns False if not found."""
    slug = pg._get_slug()
    row = pg.fetchone("SELECT id FROM tags WHERE name = %s AND tenant_slug = %s", (old_name, slug))
    if not row:
        return False
    if color:
        pg.execute("UPDATE tags SET color = %s WHERE name = %s AND tenant_slug = %s", (color, old_name, slug))
    if new_name and new_name != old_name:
        pg.execute("UPDATE tags SET name = %s WHERE name = %s AND tenant_slug = %s", (new_name, old_name, slug))
    return True


def delete(name: str) -> bool:
    """Delete a tag and remove it from all contacts. Returns False if not found."""
    slug = pg._get_slug()
    row = pg.fetchone("SELECT id FROM tags WHERE name = %s AND tenant_slug = %s", (name, slug))
    if not row:
        return False
    tag_id = row["id"]
    pg.execute("DELETE FROM contact_tags WHERE tag_id = %s AND tenant_slug = %s", (tag_id, slug))
    pg.execute("DELETE FROM tags WHERE id = %s AND tenant_slug = %s", (tag_id, slug))
    return True


def get_contact_tags(contact_id: int) -> list[str]:
    """Return tag names for a contact."""
    slug = pg._get_slug()
    rows = pg.fetchall(
        """SELECT t.name FROM tags t
           JOIN contact_tags ct ON ct.tag_id = t.id
           WHERE ct.contact_id = %s AND ct.tenant_slug = %s AND t.tenant_slug = %s
           ORDER BY t.name""",
        (contact_id, slug, slug),
    )
    return [r["name"] for r in rows]


def set_contact_tags(contact_id: int, tag_names: list[str]) -> None:
    """Replace all tags for a contact with the given list."""
    slug = pg._get_slug()
    with pg.get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM contact_tags WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug))
            for name in tag_names:
                cur.execute("SELECT id FROM tags WHERE name = %s AND tenant_slug = %s", (name, slug))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        """
                        INSERT INTO contact_tags (tenant_slug, contact_id, tag_id) 
                        VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (slug, contact_id, row[0]),
                    )
        conn.commit()


def add_contact_tag(contact_id: int, tag_name: str) -> None:
    """Add a single tag to a contact."""
    slug = pg._get_slug()
    row = pg.fetchone("SELECT id FROM tags WHERE name = %s AND tenant_slug = %s", (tag_name, slug))
    if row:
        pg.execute(
            """
            INSERT INTO contact_tags (tenant_slug, contact_id, tag_id) 
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (slug, contact_id, row["id"]),
        )


def remove_contact_tag(contact_id: int, tag_name: str) -> None:
    """Remove a single tag from a contact."""
    slug = pg._get_slug()
    row = pg.fetchone("SELECT id FROM tags WHERE name = %s AND tenant_slug = %s", (tag_name, slug))
    if row:
        pg.execute(
            "DELETE FROM contact_tags WHERE contact_id = %s AND tag_id = %s AND tenant_slug = %s",
            (contact_id, row["id"], slug),
        )
