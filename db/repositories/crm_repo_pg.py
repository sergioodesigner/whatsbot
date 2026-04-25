"""Supabase Postgres repository for simple CRM deals and tasks (Phase 3).

Drop-in equivalent of ``db.repositories.crm_repo`` backed by Postgres.
"""

from __future__ import annotations

import time

from db import tenant_pg_connection as pg
from db.repositories import contact_repo

DEFAULT_STAGES = ["novo", "em_atendimento", "proposta", "fechado_ganho", "perdido"]


def _is_crm_eligible_phone(phone: str) -> bool:
    p = str(phone or "").strip().lower()
    if not p:
        return False
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
    slug = pg._get_slug()
    rows = pg.fetchall(
        """
        SELECT id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at
        FROM crm_deals
        WHERE tenant_slug = %s
        ORDER BY updated_at DESC
        """,
        (slug,),
    )
    items = []
    for row in rows:
        item = dict(row)
        item["contact"] = _contact_snapshot(item["contact_phone"])
        items.append(item)
    return items


def get_deal(deal_id: int) -> dict | None:
    slug = pg._get_slug()
    row = pg.fetchone(
        """
        SELECT id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at
        FROM crm_deals
        WHERE id = %s AND tenant_slug = %s
        """,
        (deal_id, slug),
    )
    if not row:
        return None
    item = dict(row)
    item["contact"] = _contact_snapshot(item["contact_phone"])
    return item


def get_deal_by_phone(phone: str) -> dict | None:
    clean_phone = str(phone or "").strip()
    if not clean_phone or not _is_crm_eligible_phone(clean_phone):
        return None
    slug = pg._get_slug()
    row = pg.fetchone(
        """
        SELECT id, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at
        FROM crm_deals
        WHERE contact_phone = %s AND tenant_slug = %s
        """,
        (clean_phone, slug),
    )
    if not row:
        return None
    # Hot path for chat panel: avoid extra contact snapshot query here.
    return dict(row)


def upsert_deal(data: dict) -> dict:
    now = time.time()
    phone = str(data.get("contact_phone", "")).strip()
    if not phone:
        raise ValueError("contact_phone é obrigatório.")
    if not _is_crm_eligible_phone(phone):
        raise ValueError("Este contato não é elegível para CRM.")
    stage = str(data.get("stage", "novo")).strip() or "novo"
    if stage not in DEFAULT_STAGES:
        raise ValueError("stage inválido.")
    title = str(data.get("title", "") or "").strip()
    owner = str(data.get("owner", "") or "").strip()
    notes = str(data.get("notes", "") or "").strip()
    origin = str(data.get("origin", "manual") or "manual").strip()
    potential_value = float(data.get("potential_value") or 0.0)

    slug = pg._get_slug()

    row = pg.execute_returning(
        """
        INSERT INTO crm_deals
            (tenant_slug, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_slug, contact_phone) DO UPDATE SET
            title = EXCLUDED.title,
            stage = EXCLUDED.stage,
            origin = EXCLUDED.origin,
            potential_value = EXCLUDED.potential_value,
            owner = EXCLUDED.owner,
            notes = EXCLUDED.notes,
            updated_at = EXCLUDED.updated_at
        RETURNING id
        """,
        (slug, phone, title, stage, origin, potential_value, owner, notes, now, now),
    )
    return get_deal(row["id"]) if row else {}


def touch_or_create_from_contact(phone: str, *, suggested_title: str = "") -> dict:
    clean_phone = str(phone or "").strip()
    if not _is_crm_eligible_phone(clean_phone):
        raise ValueError("Este contato não é elegível para CRM.")
    now = time.time()
    slug = pg._get_slug()
    
    # Try getting the existing deal id
    existing = pg.fetchone(
        "SELECT id FROM crm_deals WHERE contact_phone = %s AND tenant_slug = %s",
        (clean_phone, slug),
    )
    if existing:
        pg.execute(
            "UPDATE crm_deals SET updated_at = %s WHERE id = %s",
            (now, existing["id"]),
        )
        return get_deal(existing["id"]) or {}
        
    contact = contact_repo.get_by_phone(clean_phone)
    title = str(suggested_title or "").strip()
    if not title and contact:
        title = str(contact.get("name") or "").strip()

    row = pg.execute_returning(
        """
        INSERT INTO crm_deals
            (tenant_slug, contact_phone, title, stage, origin, potential_value, owner, notes, created_at, updated_at)
        VALUES (%s, %s, %s, 'novo', 'whatsapp_auto', 0, '', '', %s, %s)
        RETURNING id
        """,
        (slug, clean_phone, title, now, now),
    )
    return get_deal(row["id"]) if row else {}


def update_deal(deal_id: int, data: dict) -> dict | None:
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

    slug = pg._get_slug()
    pg.execute(
        """
        UPDATE crm_deals
        SET contact_phone = %s, title = %s, stage = %s, origin = %s, potential_value = %s, owner = %s, notes = %s, updated_at = %s
        WHERE id = %s AND tenant_slug = %s
        """,
        (
            payload["contact_phone"],
            str(payload["title"] or "").strip(),
            payload["stage"],
            str(payload["origin"] or "manual").strip(),
            float(payload["potential_value"] or 0.0),
            str(payload["owner"] or "").strip(),
            str(payload["notes"] or "").strip(),
            time.time(),
            deal_id,
            slug,
        ),
    )
    return get_deal(deal_id)


def list_tasks(deal_id: int) -> list[dict]:
    slug = pg._get_slug()
    return pg.fetchall(
        """
        SELECT id, deal_id, title, due_ts, done, notes, created_at, updated_at
        FROM crm_tasks
        WHERE deal_id = %s AND tenant_slug = %s
        ORDER BY done ASC, COALESCE(due_ts, 32503680000) ASC, created_at ASC
        """,
        (deal_id, slug),
    )


def create_task(deal_id: int, data: dict) -> dict:
    title = str(data.get("title", "") or "").strip()
    if not title:
        raise ValueError("title é obrigatório.")
    due_ts = float(data.get("due_ts")) if data.get("due_ts") else None
    done = 1 if bool(data.get("done")) else 0
    notes = str(data.get("notes", "") or "").strip()
    now = time.time()
    slug = pg._get_slug()

    row = pg.execute_returning(
        """
        INSERT INTO crm_tasks
            (tenant_slug, deal_id, title, due_ts, done, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, deal_id, title, due_ts, done, notes, created_at, updated_at
        """,
        (slug, deal_id, title, due_ts, done, notes, now, now),
    )
    return row or {}


def update_task(task_id: int, data: dict) -> dict | None:
    slug = pg._get_slug()
    current = pg.fetchone(
        "SELECT id, deal_id, title, due_ts, done, notes, created_at, updated_at FROM crm_tasks WHERE id = %s AND tenant_slug = %s",
        (task_id, slug),
    )
    if not current:
        return None
    title = str(data.get("title", current["title"]) or "").strip()
    if not title:
        raise ValueError("title é obrigatório.")
    due_ts = float(data.get("due_ts")) if data.get("due_ts") else current.get("due_ts")
    done = 1 if bool(data.get("done", bool(current.get("done")))) else 0
    notes = str(data.get("notes", current.get("notes", "")) or "").strip()

    row = pg.execute_returning(
        """
        UPDATE crm_tasks
        SET title = %s, due_ts = %s, done = %s, notes = %s, updated_at = %s
        WHERE id = %s AND tenant_slug = %s
        RETURNING id, deal_id, title, due_ts, done, notes, created_at, updated_at
        """,
        (title, due_ts, done, notes, time.time(), task_id, slug),
    )
    return row


def delete_task(task_id: int) -> bool:
    slug = pg._get_slug()
    rows = pg.execute("DELETE FROM crm_tasks WHERE id = %s AND tenant_slug = %s", (task_id, slug))
    return rows > 0
