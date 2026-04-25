"""Supabase Postgres repository for contacts table (Phase 4).

Drop-in equivalent of ``db.repositories.contact_repo`` backed by Postgres.
"""

from __future__ import annotations

import time

from db import tenant_pg_connection as pg


def _br_phone_variants(phone: str) -> list[str]:
    """Return phone number variants for Brazilian numbers."""
    if not phone or not phone.startswith("55"):
        return [phone]
    if len(phone) == 13 and phone[4] == "9":
        alt = phone[:4] + phone[5:]
        return [phone, alt]
    if len(phone) == 12:
        alt = phone[:4] + "9" + phone[4:]
        return [phone, alt]
    return [phone]


def get_or_create(phone: str, default_ai_enabled: bool = True) -> dict:
    slug = pg._get_slug()
    variants = _br_phone_variants(phone)
    placeholders = ",".join("%s" for _ in variants)
    row = pg.fetchone(
        f"SELECT * FROM contacts WHERE tenant_slug = %s AND phone IN ({placeholders})",
        [slug] + variants
    )
    if row is not None:
        return _row_to_dict(row)
    now = time.time()
    cur_row = pg.execute_returning(
        """INSERT INTO contacts (tenant_slug, phone, ai_enabled, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (slug, phone, 1 if default_ai_enabled else 0, now, now),
    )
    return {
        "id": cur_row["id"],
        "phone": phone,
        "name": "",
        "email": "",
        "profession": "",
        "company": "",
        "address": "",
        "ai_enabled": default_ai_enabled,
        "is_group": False,
        "group_name": "",
        "is_archived": False,
        "archived_by_app": False,
        "can_send": True,
        "unread_count": 0,
        "unread_ai_count": 0,
        "created_at": now,
        "updated_at": now,
    }


def delete(contact_id: int) -> None:
    slug = pg._get_slug()
    pg.execute("DELETE FROM contacts WHERE id = %s AND tenant_slug = %s", (contact_id, slug))


def set_archived(contact_id: int, archived: bool, by_app: bool = False) -> None:
    slug = pg._get_slug()
    pg.execute(
        "UPDATE contacts SET is_archived = %s, archived_by_app = %s, updated_at = %s WHERE id = %s AND tenant_slug = %s",
        (1 if archived else 0, 1 if (archived and by_app) else 0, time.time(), contact_id, slug),
    )


def get_by_phone(phone: str) -> dict | None:
    slug = pg._get_slug()
    variants = _br_phone_variants(phone)
    placeholders = ",".join("%s" for _ in variants)
    row = pg.fetchone(
        f"SELECT * FROM contacts WHERE tenant_slug = %s AND phone IN ({placeholders})",
        [slug] + variants
    )
    if row is None:
        return None
    return _row_to_dict(row)


def get_by_id(contact_id: int) -> dict | None:
    slug = pg._get_slug()
    row = pg.fetchone(
        "SELECT * FROM contacts WHERE id = %s AND tenant_slug = %s",
        (contact_id, slug),
    )
    if row is None:
        return None
    return _row_to_dict(row)


def update(contact_id: int, **fields) -> None:
    if not fields:
        return
    slug = pg._get_slug()
    fields["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [contact_id, slug]
    pg.execute(f"UPDATE contacts SET {set_clause} WHERE id = %s AND tenant_slug = %s", values)


def increment_unread(contact_id: int, msg_id: str | None = None) -> None:
    slug = pg._get_slug()
    with pg.dict_cursor() as (conn, cur):
        cur.execute(
            "UPDATE contacts SET unread_count = unread_count + 1, updated_at = %s WHERE id = %s AND tenant_slug = %s",
            (time.time(), contact_id, slug),
        )
        if msg_id:
            cur.execute(
                "INSERT INTO unread_msg_ids (tenant_slug, contact_id, msg_id) VALUES (%s, %s, %s)",
                (slug, contact_id, msg_id),
            )
        conn.commit()


def increment_unread_ai(contact_id: int) -> None:
    slug = pg._get_slug()
    pg.execute(
        "UPDATE contacts SET unread_ai_count = unread_ai_count + 1, updated_at = %s WHERE id = %s AND tenant_slug = %s",
        (time.time(), contact_id, slug),
    )


def mark_as_read(contact_id: int) -> list[str]:
    slug = pg._get_slug()
    with pg.dict_cursor() as (conn, cur):
        cur.execute(
            "SELECT msg_id FROM unread_msg_ids WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug)
        )
        rows = cur.fetchall()
        msg_ids = [r["msg_id"] for r in rows]
        cur.execute("DELETE FROM unread_msg_ids WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug))
        cur.execute(
            "UPDATE contacts SET unread_count = 0, unread_ai_count = 0, updated_at = %s WHERE id = %s AND tenant_slug = %s",
            (time.time(), contact_id, slug),
        )
        conn.commit()
    return msg_ids


def mark_user_messages_as_read(contact_id: int) -> list[str]:
    slug = pg._get_slug()
    with pg.dict_cursor() as (conn, cur):
        cur.execute(
            "SELECT msg_id FROM unread_msg_ids WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug)
        )
        rows = cur.fetchall()
        msg_ids = [r["msg_id"] for r in rows]
        cur.execute("DELETE FROM unread_msg_ids WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug))
        cur.execute(
            "UPDATE contacts SET unread_count = 0, updated_at = %s WHERE id = %s AND tenant_slug = %s",
            (time.time(), contact_id, slug),
        )
        conn.commit()
    return msg_ids


def get_unread_msg_ids(contact_id: int) -> list[str]:
    """Return tracked unread msg_ids for a contact (without clearing)."""
    slug = pg._get_slug()
    rows = pg.fetchall(
        "SELECT msg_id FROM unread_msg_ids WHERE contact_id = %s AND tenant_slug = %s",
        (contact_id, slug),
    )
    return [r["msg_id"] for r in rows]


def get_observations(contact_id: int) -> list[str]:
    slug = pg._get_slug()
    rows = pg.fetchall(
        "SELECT text FROM observations WHERE contact_id = %s AND tenant_slug = %s ORDER BY created_at",
        (contact_id, slug),
    )
    return [r["text"] for r in rows]


def set_observations(contact_id: int, observations: list[str]) -> None:
    slug = pg._get_slug()
    with pg.dict_cursor() as (conn, cur):
        cur.execute("DELETE FROM observations WHERE contact_id = %s AND tenant_slug = %s", (contact_id, slug))
        now = time.time()
        for text in observations:
            if text.strip():
                cur.execute(
                    "INSERT INTO observations (tenant_slug, contact_id, text, created_at) VALUES (%s, %s, %s, %s)",
                    (slug, contact_id, text, now)
                )
        conn.commit()


def add_observation(contact_id: int, text: str) -> None:
    slug = pg._get_slug()
    existing = pg.fetchone(
        "SELECT 1 FROM observations WHERE contact_id = %s AND text = %s AND tenant_slug = %s",
        (contact_id, text, slug),
    )
    if existing:
        return
    pg.execute(
        "INSERT INTO observations (tenant_slug, contact_id, text, created_at) VALUES (%s, %s, %s, %s)",
        (slug, contact_id, text, time.time()),
    )


def list_contacts(q: str = "", archived: bool = False) -> list[dict]:
    slug = pg._get_slug()
    rows = pg.fetchall(
        """
        SELECT c.*,
               lm.content   AS last_msg_content,
               lm.role      AS last_msg_role,
               lm.ts        AS last_msg_ts,
               lm.media_type AS last_msg_media_type,
               lm.status    AS last_msg_status,
               lm.msg_id    AS last_msg_id,
               (SELECT COUNT(*) FROM messages WHERE contact_id = c.id AND tenant_slug = %s) AS msg_count,
               COALESCE(tag_agg.tags, ARRAY[]::TEXT[]) AS tags
        FROM contacts c
        LEFT JOIN (
            SELECT m1.contact_id, m1.content, m1.role, m1.ts, m1.media_type, m1.status, m1.msg_id
            FROM messages m1
            INNER JOIN (
                SELECT contact_id, MAX(ts) AS max_ts
                FROM messages
                WHERE role NOT IN ('transcription', 'system_notice') AND tenant_slug = %s
                GROUP BY contact_id
            ) m2 ON m1.contact_id = m2.contact_id AND m1.ts = m2.max_ts
            WHERE m1.tenant_slug = %s
        ) lm ON lm.contact_id = c.id
        LEFT JOIN LATERAL (
            SELECT ARRAY_AGG(t.name ORDER BY t.name) AS tags
            FROM contact_tags ct
            JOIN tags t ON t.id = ct.tag_id
            WHERE ct.contact_id = c.id
              AND ct.tenant_slug = %s
              AND t.tenant_slug = %s
        ) tag_agg ON TRUE
        WHERE c.is_archived = %s
          AND c.tenant_slug = %s
          AND (c.phone NOT LIKE '%%@%%' OR c.phone LIKE '%%@g.us')
        ORDER BY COALESCE(lm.ts, c.updated_at) DESC
        """,
        (slug, slug, slug, slug, slug, 1 if archived else 0, slug),
    )

    results = []
    for row in rows:
        contact_id = row["id"]
        tags = [t for t in (row.get("tags") or []) if t]

        last_content = ""
        lmt = row["last_msg_media_type"]
        if row["last_msg_content"] is not None:
            if lmt == "image":
                last_content = (row["last_msg_content"] or "")[:80] or "\U0001f4f7 Imagem"
            elif lmt == "audio":
                last_content = "\U0001f3a4 Áudio"
            elif lmt == "gif":
                last_content = "\U0001f39e\ufe0f GIF"
            elif lmt == "video":
                last_content = "\U0001f3ac Vídeo"
            else:
                last_content = (row["last_msg_content"] or "")[:80]

        is_group = bool(row["is_group"])
        group_name = row["group_name"] or ""
        name = group_name if is_group else (row["name"] or "")

        results.append({
            "id": contact_id,
            "phone": row["phone"],
            "name": name,
            "last_message": last_content,
            "last_message_role": row["last_msg_role"] or "",
            "last_message_ts": row["last_msg_ts"] or 0,
            "last_message_status": row["last_msg_status"] or "",
            "last_message_msg_id": row["last_msg_id"] or "",
            "msg_count": row["msg_count"] or 0,
            "unread_count": row["unread_count"],
            "unread_ai_count": row["unread_ai_count"],
            "ai_enabled": bool(row["ai_enabled"]),
            "is_group": is_group,
            "group_name": group_name,
            "is_archived": bool(row["is_archived"]),
            "archived_by_app": bool(row["archived_by_app"]) if row["archived_by_app"] is not None else False,
            "can_send": bool(row["can_send"]) if row["can_send"] is not None else True,
            "tags": tags,
            "updated_at": row["updated_at"],
        })

    if q:
        ql = q.lower()
        results = [
            c for c in results
            if ql in c["name"].lower()
            or ql in c["phone"]
            or ql in c.get("group_name", "").lower()
            or any(ql in t.lower() for t in c.get("tags", []))
        ]

    return results


def get_full_contact(phone: str) -> dict | None:
    slug = pg._get_slug()
    variants = _br_phone_variants(phone)
    placeholders = ",".join("%s" for _ in variants)
    row = pg.fetchone(
        f"SELECT * FROM contacts WHERE tenant_slug = %s AND phone IN ({placeholders})",
        [slug] + variants
    )
    if row is None:
        return None

    contact_id = row["id"]
    observations = get_observations(contact_id)

    with pg.dict_cursor() as (conn, cur):
        cur.execute(
            """SELECT t.name FROM tags t
               JOIN contact_tags ct ON ct.tag_id = t.id
               WHERE ct.contact_id = %s AND ct.tenant_slug = %s AND t.tenant_slug = %s""",
            (contact_id, slug, slug),
        )
        tag_rows = cur.fetchall()
        tags = [t["name"] for t in tag_rows]

    data = _row_to_dict(row)
    data["info"] = {
        "name": row["name"],
        "email": row["email"],
        "profession": row["profession"],
        "company": row["company"],
        "address": row["address"],
        "observations": observations,
    }
    data["tags"] = tags
    return data


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "phone": row["phone"],
        "name": row["name"],
        "email": row["email"],
        "profession": row["profession"],
        "company": row["company"],
        "address": row["address"],
        "ai_enabled": bool(row["ai_enabled"]),
        "is_group": bool(row["is_group"]),
        "group_name": row["group_name"],
        "is_archived": bool(row["is_archived"]),
        "archived_by_app": bool(row["archived_by_app"]) if row["archived_by_app"] is not None else False,
        "can_send": bool(row["can_send"]) if row["can_send"] is not None else True,
        "unread_count": row["unread_count"],
        "unread_ai_count": row["unread_ai_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
