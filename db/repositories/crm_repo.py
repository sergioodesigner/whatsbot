"""Repository for simple CRM deals and tasks (tenant DB)."""

import time

from db.connection import get_db
from db.repositories import contact_repo

DEFAULT_STAGES = ["novo", "em_atendimento", "proposta", "fechado_ganho", "perdido"]
_SCHEMA_READY = False


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn = get_db()
    cols = conn.execute("PRAGMA table_info(crm_deals)").fetchall()
    col_names = {c["name"] for c in cols}
    if "origin" not in col_names:
        conn.execute("ALTER TABLE crm_deals ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")
        conn.commit()
    _SCHEMA_READY = True


def _is_crm_eligible_phone(phone: str) -> bool:
    p = str(phone or "").strip().lower()
    if not p:
        return False
    # Exclude groups/channels/broadcasts from CRM context.
    if "@g.us" in p or "@newsletter" in p or "@broadcast" in p:
        return False
    return True


def _contact_snapshot(phone: str) -> dict:
    c = contact_repo.get_by_phone(phone)
    if not c:
        return {"id": None, "phone": phone, "name": "", "company": "", "email": "", "profession": "", "observations": []}
    return {
        "id": c["id"],
        "phone": c["phone"],
        "name": c.get("name", ""),
        "company": c.get("company", ""),
        "email": c.get("email", ""),
        "profession": c.get("profession", ""),
        "observations": contact_repo.get_observations(c["id"]),
    }


def list_deals() -> list[dict]:
    _ensure_schema()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, contact_id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at
        FROM crm_deals
        ORDER BY updated_at DESC
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["contact"] = _contact_snapshot(item["contact_phone"])
        items.append(item)
    return items


def get_deal(deal_id: int) -> dict | None:
    _ensure_schema()
    conn = get_db()
    row = conn.execute(
        """
        SELECT id, contact_id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at
        FROM crm_deals
        WHERE id = ?
        """,
        (deal_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["contact"] = _contact_snapshot(item["contact_phone"])
    return item


def get_deal_by_phone(phone: str) -> dict | None:
    _ensure_schema()
    clean_phone = str(phone or "").strip()
    if not clean_phone or not _is_crm_eligible_phone(clean_phone):
        return None
    conn = get_db()
    row = conn.execute(
        """
        SELECT id, contact_id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at
        FROM crm_deals
        WHERE contact_phone = ?
        """,
        (clean_phone,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["contact"] = _contact_snapshot(item["contact_phone"])
    return item


def upsert_deal(data: dict) -> dict:
    _ensure_schema()
    conn = get_db()
    now = time.time()
    phone = str(data.get("contact_phone", "")).strip()
    if not phone:
        raise ValueError("contact_phone é obrigatório.")
    if not _is_crm_eligible_phone(phone):
        raise ValueError("Este contato não é elegível para CRM.")
    stage = str(data.get("stage", "novo")).strip() or "novo"
    if stage not in DEFAULT_STAGES:
        raise ValueError("stage inválido.")
    contact = contact_repo.get_by_phone(phone)
    contact_id = contact["id"] if contact else None
    title = str(data.get("title", "") or "").strip()
    owner = str(data.get("owner", "") or "").strip()
    notes = str(data.get("notes", "") or "").strip()
    origin = str(data.get("origin", "manual") or "manual").strip()
    potential_value = float(data.get("potential_value") or 0.0)

    existing = conn.execute("SELECT id FROM crm_deals WHERE contact_phone = ?", (phone,)).fetchone()
    if existing:
        deal_id = int(existing["id"])
        conn.execute(
            """
            UPDATE crm_deals
            SET contact_id = ?, title = ?, stage = ?, origin = ?, potential_value = ?, owner = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (contact_id, title, stage, origin, potential_value, owner, notes, now, deal_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO crm_deals (contact_id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (contact_id, phone, title, stage, origin, potential_value, owner, notes, now, now),
        )
        deal_id = int(cur.lastrowid)
    conn.commit()
    deal = get_deal(deal_id)
    return deal or {}


def touch_or_create_from_contact(phone: str, *, suggested_title: str = "") -> dict:
    """Create deal if missing from contact; otherwise only refresh timestamp."""
    _ensure_schema()
    clean_phone = str(phone or "").strip()
    if not _is_crm_eligible_phone(clean_phone):
        raise ValueError("Este contato não é elegível para CRM.")
    now = time.time()
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM crm_deals WHERE contact_phone = ?",
        (clean_phone,),
    ).fetchone()
    contact = contact_repo.get_by_phone(clean_phone)
    contact_id = contact["id"] if contact else None
    if existing:
        deal_id = int(existing["id"])
        conn.execute(
            "UPDATE crm_deals SET contact_id = ?, updated_at = ? WHERE id = ?",
            (contact_id, now, deal_id),
        )
    else:
        title = str(suggested_title or "").strip()
        if not title and contact:
            title = str(contact.get("name") or "").strip()
        cur = conn.execute(
            """
            INSERT INTO crm_deals (contact_id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at)
            VALUES (?, ?, ?, 'novo', 'whatsapp_auto', 0, '', '', ?, ?)
            """,
            (contact_id, clean_phone, title, now, now),
        )
        deal_id = int(cur.lastrowid)
    conn.commit()
    return get_deal(deal_id) or {}


def update_deal(deal_id: int, data: dict) -> dict | None:
    _ensure_schema()
    current = get_deal(deal_id)
    if not current:
        return None
    payload = {
        "contact_phone": data.get("contact_phone", current["contact_phone"]),
        "title": data.get("title", current["title"]),
        "stage": data.get("stage", current["stage"]),
        "potential_value": data.get("potential_value", current["potential_value"]),
        "owner": data.get("owner", current["owner"]),
        "notes": data.get("notes", current["notes"]),
        "origin": data.get("origin", current.get("origin", "manual")),
    }
    payload["contact_phone"] = str(payload["contact_phone"]).strip()
    payload["stage"] = str(payload["stage"]).strip() or "novo"
    if payload["stage"] not in DEFAULT_STAGES:
        raise ValueError("stage inválido.")

    conn = get_db()
    contact = contact_repo.get_by_phone(payload["contact_phone"])
    contact_id = contact["id"] if contact else None
    conn.execute(
        """
        UPDATE crm_deals
        SET contact_id = ?, contact_phone = ?, title = ?, stage = ?, origin = ?, potential_value = ?, owner = ?, notes = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            contact_id,
            payload["contact_phone"],
            str(payload["title"] or "").strip(),
            payload["stage"],
            str(payload["origin"] or "manual").strip(),
            float(payload["potential_value"] or 0.0),
            str(payload["owner"] or "").strip(),
            str(payload["notes"] or "").strip(),
            time.time(),
            deal_id,
        ),
    )
    conn.commit()
    return get_deal(deal_id)


def list_tasks(deal_id: int) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, deal_id, title, due_ts, done, notes, created_at, updated_at
        FROM crm_tasks
        WHERE deal_id = ?
        ORDER BY done ASC, COALESCE(due_ts, 32503680000) ASC, created_at ASC
        """,
        (deal_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_task(deal_id: int, data: dict) -> dict:
    title = str(data.get("title", "") or "").strip()
    if not title:
        raise ValueError("title é obrigatório.")
    due_ts = float(data.get("due_ts")) if data.get("due_ts") else None
    done = 1 if bool(data.get("done")) else 0
    notes = str(data.get("notes", "") or "").strip()
    now = time.time()
    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO crm_tasks (deal_id, title, due_ts, done, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (deal_id, title, due_ts, done, notes, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, deal_id, title, due_ts, done, notes, created_at, updated_at FROM crm_tasks WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return dict(row) if row else {}


def update_task(task_id: int, data: dict) -> dict | None:
    conn = get_db()
    current = conn.execute(
        "SELECT id, deal_id, title, due_ts, done, notes, created_at, updated_at FROM crm_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not current:
        return None
    current = dict(current)
    title = str(data.get("title", current["title"]) or "").strip()
    if not title:
        raise ValueError("title é obrigatório.")
    due_ts = float(data.get("due_ts")) if data.get("due_ts") else current.get("due_ts")
    done = 1 if bool(data.get("done", bool(current.get("done")))) else 0
    notes = str(data.get("notes", current.get("notes", "")) or "").strip()
    conn.execute(
        """
        UPDATE crm_tasks
        SET title = ?, due_ts = ?, done = ?, notes = ?, updated_at = ?
        WHERE id = ?
        """,
        (title, due_ts, done, notes, time.time(), task_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id, deal_id, title, due_ts, done, notes, created_at, updated_at FROM crm_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_task(task_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM crm_tasks WHERE id = ?", (task_id,))
    conn.commit()
    return cur.rowcount > 0
