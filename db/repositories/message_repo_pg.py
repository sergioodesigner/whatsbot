"""Supabase Postgres repository for messages table (Phase 4).

Drop-in equivalent of ``db.repositories.message_repo`` backed by Postgres.
"""

import time

from db import tenant_pg_connection as pg


def add(contact_id: int, role: str, content: str, *,
        media_type: str | None = None, media_path: str | None = None,
        status: str | None = None, msg_id: str | None = None,
        ts: float | None = None) -> dict:
    """Insert a message and return it as a dict."""
    slug = pg._get_slug()
    ts = ts or time.time()
    row = pg.execute_returning(
        """INSERT INTO messages (tenant_slug, contact_id, role, content, ts, media_type, media_path, status, msg_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (slug, contact_id, role, content, ts, media_type, media_path, status, msg_id),
    )
    return {
        "id": row["id"] if row else 0,
        "role": role,
        "content": content,
        "ts": ts,
        "media_type": media_type,
        "media_path": media_path,
        "status": status,
        "msg_id": msg_id,
    }


def get_all(contact_id: int) -> list[dict]:
    """Return all messages for a contact ordered by timestamp."""
    slug = pg._get_slug()
    rows = pg.fetchall(
        "SELECT * FROM messages WHERE contact_id = %s AND tenant_slug = %s ORDER BY ts",
        (contact_id, slug),
    )
    return [_row_to_dict(r) for r in rows]


def get_recent(contact_id: int, limit: int) -> list[dict]:
    """Return the last *limit* messages for a contact, oldest first."""
    if limit <= 0:
        return []
    slug = pg._get_slug()
    rows = pg.fetchall(
        """SELECT * FROM (
               SELECT * FROM messages
               WHERE contact_id = %s AND tenant_slug = %s
               ORDER BY ts DESC LIMIT %s
           ) sub ORDER BY ts ASC""",
        (contact_id, slug, limit),
    )
    return [_row_to_dict(r) for r in rows]


def get_context(contact_id: int, limit: int) -> list[dict]:
    """Return the last N eligible messages for LLM context."""
    slug = pg._get_slug()
    rows = pg.fetchall(
        """SELECT * FROM messages
           WHERE contact_id = %s AND tenant_slug = %s
             AND role NOT IN ('transcription', 'tool_call', 'system_notice')
             AND (status IS NULL OR status != 'failed')
           ORDER BY ts DESC
           LIMIT %s""",
        (contact_id, slug, limit),
    )
    # Reverse to get chronological order
    return [_row_to_dict(r) for r in reversed(rows)]


def get_last(contact_id: int) -> dict | None:
    """Return the most recent message for a contact."""
    slug = pg._get_slug()
    row = pg.fetchone(
        "SELECT * FROM messages WHERE contact_id = %s AND tenant_slug = %s ORDER BY ts DESC LIMIT 1",
        (contact_id, slug),
    )
    return _row_to_dict(row) if row else None


def get_last_user_message(contact_id: int) -> dict | None:
    """Return the most recent user message."""
    slug = pg._get_slug()
    row = pg.fetchone(
        """SELECT * FROM messages
           WHERE contact_id = %s AND role = 'user' AND tenant_slug = %s
           ORDER BY ts DESC LIMIT 1""",
        (contact_id, slug),
    )
    return _row_to_dict(row) if row else None


def update_content(message_id: int, content: str) -> None:
    """Update the content of a specific message."""
    slug = pg._get_slug()
    pg.execute("UPDATE messages SET content = %s WHERE id = %s AND tenant_slug = %s", (content, message_id, slug))


def update_status(contact_id: int, content: str, new_status: str | None,
                   msg_id: str | None = None) -> None:
    """Update status of the most recent message matching content (for retry-send)."""
    slug = pg._get_slug()
    with pg.dict_cursor() as (conn, cur):
        cur.execute(
            """SELECT id FROM messages
               WHERE contact_id = %s AND content = %s AND status = 'failed' AND tenant_slug = %s
               ORDER BY ts DESC LIMIT 1""",
            (contact_id, content, slug),
        )
        row = cur.fetchone()
        if row:
            if msg_id:
                cur.execute(
                    "UPDATE messages SET status = %s, msg_id = %s WHERE id = %s AND tenant_slug = %s",
                    (new_status, msg_id, row["id"], slug),
                )
            else:
                cur.execute(
                    "UPDATE messages SET status = %s WHERE id = %s AND tenant_slug = %s",
                    (new_status, row["id"], slug),
                )
        conn.commit()


def update_status_by_msg_id(msg_id: str, new_status: str) -> list[str]:
    """Update delivery status by GOWA msg_id."""
    slug = pg._get_slug()
    updated_msg_ids = []

    with pg.dict_cursor() as (conn, cur):
        cur.execute(
            """UPDATE messages SET status = %s
               WHERE msg_id = %s AND tenant_slug = %s
                 AND status IS NOT NULL
                 AND status IN ('sent', 'delivered', 'operator')
               RETURNING msg_id""",
            (new_status, msg_id, slug),
        )
        if cur.rowcount > 0:
            updated_msg_ids.append(msg_id)

        cur.execute(
            "SELECT contact_id, ts FROM messages WHERE msg_id = %s AND tenant_slug = %s",
            (msg_id, slug),
        )
        row = cur.fetchone()
        if row:
            c_id, c_ts = row["contact_id"], row["ts"]
            prior_statuses = ('sent', 'operator') if new_status == 'delivered' else ('sent', 'delivered', 'operator')
            placeholders = ','.join('%s' for _ in prior_statuses)
            
            cur.execute(
                f"""SELECT msg_id FROM messages
                    WHERE contact_id = %s AND role = 'assistant' AND tenant_slug = %s
                      AND ts <= %s AND status IN ({placeholders})
                      AND msg_id IS NOT NULL AND msg_id != %s""",
                (c_id, slug, c_ts, *prior_statuses, msg_id),
            )
            prior_rows = cur.fetchall()
            cascaded_ids = [r["msg_id"] for r in prior_rows]
            
            if cascaded_ids:
                cur.execute(
                    f"""UPDATE messages SET status = %s
                        WHERE contact_id = %s AND role = 'assistant' AND tenant_slug = %s
                          AND ts <= %s AND status IN ({placeholders})""",
                    (new_status, c_id, slug, c_ts, *prior_statuses),
                )
                updated_msg_ids.extend(cascaded_ids)
        conn.commit()
    return updated_msg_ids


def get_contact_id_by_msg_id(msg_id: str) -> int | None:
    """Look up the contact_id for a given GOWA msg_id."""
    slug = pg._get_slug()
    row = pg.fetchone(
        "SELECT contact_id FROM messages WHERE msg_id = %s AND tenant_slug = %s LIMIT 1",
        (msg_id, slug),
    )
    return row["contact_id"] if row else None


def update_msg_id_and_status(message_id: int, msg_id: str, status: str) -> None:
    """Set msg_id and status on a message (used after retry-send)."""
    slug = pg._get_slug()
    pg.execute(
        "UPDATE messages SET msg_id = %s, status = %s WHERE id = %s AND tenant_slug = %s",
        (msg_id, status, message_id, slug),
    )


def delete_all(contact_id: int) -> None:
    """Delete all messages for a contact."""
    slug = pg._get_slug()
    pg.execute("DELETE FROM messages WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug))


def _row_to_dict(row) -> dict:
    d = {
        "role": row["role"],
        "content": row["content"],
        "ts": row["ts"],
        "status": row["status"],
        "msg_id": row["msg_id"],
    }
    if row.get("media_type"):
        d["media_type"] = row["media_type"]
    if row.get("media_path"):
        d["media_path"] = row["media_path"]
    d["_id"] = row["id"]
    return d
