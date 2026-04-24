"""Supabase Postgres repository for tenant billing/company profile (Phase 2).

Drop-in equivalent of ``db.repositories.master_billing_repo``.
"""

from __future__ import annotations

import time
from datetime import datetime

from db import master_pg_connection as pg


# ── Company profile ───────────────────────────────────────────────────

def get_profile(tenant_slug: str) -> dict:
    row = pg.fetchone(
        """
        SELECT tenant_slug, owner_name, owner_phone, plan_name, plan_amount,
               due_day, contract_start_ts, contract_end_ts, notes, updated_at
        FROM tenant_company_profile
        WHERE tenant_slug = %s
        """,
        (tenant_slug,),
    )
    if not row:
        return {
            "tenant_slug": tenant_slug,
            "owner_name": "",
            "owner_phone": "",
            "plan_name": "",
            "plan_amount": 0.0,
            "due_day": 10,
            "contract_start_ts": None,
            "contract_end_ts": None,
            "notes": "",
            "updated_at": None,
        }
    return row


def upsert_profile(tenant_slug: str, data: dict) -> dict:
    now = time.time()
    current = get_profile(tenant_slug)
    payload = {
        "owner_name":         data.get("owner_name", current.get("owner_name", "")),
        "owner_phone":        data.get("owner_phone", current.get("owner_phone", "")),
        "plan_name":          data.get("plan_name", current.get("plan_name", "")),
        "plan_amount":        float(data.get("plan_amount", current.get("plan_amount", 0.0)) or 0.0),
        "due_day":            int(data.get("due_day", current.get("due_day", 10)) or 10),
        "contract_start_ts":  data.get("contract_start_ts", current.get("contract_start_ts")),
        "contract_end_ts":    data.get("contract_end_ts", current.get("contract_end_ts")),
        "notes":              data.get("notes", current.get("notes", "")),
    }
    pg.execute(
        """
        INSERT INTO tenant_company_profile
            (tenant_slug, owner_name, owner_phone, plan_name, plan_amount,
             due_day, contract_start_ts, contract_end_ts, notes, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_slug) DO UPDATE SET
            owner_name         = EXCLUDED.owner_name,
            owner_phone        = EXCLUDED.owner_phone,
            plan_name          = EXCLUDED.plan_name,
            plan_amount        = EXCLUDED.plan_amount,
            due_day            = EXCLUDED.due_day,
            contract_start_ts  = EXCLUDED.contract_start_ts,
            contract_end_ts    = EXCLUDED.contract_end_ts,
            notes              = EXCLUDED.notes,
            updated_at         = EXCLUDED.updated_at
        """,
        (
            tenant_slug,
            payload["owner_name"],
            payload["owner_phone"],
            payload["plan_name"],
            payload["plan_amount"],
            payload["due_day"],
            payload["contract_start_ts"],
            payload["contract_end_ts"],
            payload["notes"],
            now,
        ),
    )
    return get_profile(tenant_slug)


# ── Invoices ──────────────────────────────────────────────────────────

def list_invoices(tenant_slug: str) -> list[dict]:
    return pg.fetchall(
        """
        SELECT id, tenant_slug, period_ym, due_ts, amount, paid, paid_at, notes
        FROM tenant_billing_invoices
        WHERE tenant_slug = %s
        ORDER BY period_ym DESC
        """,
        (tenant_slug,),
    )


def _period_to_date(period_ym: str) -> datetime:
    return datetime.strptime(f"{period_ym}-01", "%Y-%m-%d")


def _next_period(period_ym: str) -> str:
    d = _period_to_date(period_ym)
    year  = d.year + (1 if d.month == 12 else 0)
    month = 1 if d.month == 12 else d.month + 1
    return f"{year:04d}-{month:02d}"


def _period_from_ts(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d.year:04d}-{d.month:02d}"


def _due_ts_for_period(period_ym: str, due_day: int) -> float:
    due_day = min(max(int(due_day or 10), 1), 28)
    return datetime.strptime(f"{period_ym}-{due_day:02d}", "%Y-%m-%d").timestamp()


def upsert_invoice(tenant_slug: str, data: dict) -> dict:
    period_ym = str(data.get("period_ym", "")).strip()
    if not period_ym:
        raise ValueError("period_ym é obrigatório.")
    due_ts  = float(data.get("due_ts") or 0)
    amount  = float(data.get("amount") or 0.0)
    paid    = 1 if bool(data.get("paid")) else 0
    paid_at = float(data.get("paid_at")) if data.get("paid_at") else (time.time() if paid else None)
    notes   = str(data.get("notes", "") or "")

    pg.execute(
        """
        INSERT INTO tenant_billing_invoices
            (tenant_slug, period_ym, due_ts, amount, paid, paid_at, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_slug, period_ym) DO UPDATE SET
            due_ts  = EXCLUDED.due_ts,
            amount  = EXCLUDED.amount,
            paid    = EXCLUDED.paid,
            paid_at = EXCLUDED.paid_at,
            notes   = EXCLUDED.notes
        """,
        (tenant_slug, period_ym, due_ts, amount, paid, paid_at, notes),
    )
    row = pg.fetchone(
        """
        SELECT id, tenant_slug, period_ym, due_ts, amount, paid, paid_at, notes
        FROM tenant_billing_invoices
        WHERE tenant_slug = %s AND period_ym = %s
        """,
        (tenant_slug, period_ym),
    )
    return row or {}


def delete_invoice(tenant_slug: str, period_ym: str) -> bool:
    rows = pg.execute(
        "DELETE FROM tenant_billing_invoices WHERE tenant_slug = %s AND period_ym = %s",
        (tenant_slug, period_ym),
    )
    return rows > 0


def ensure_next_three_open_invoices(tenant_slug: str) -> None:
    profile = get_profile(tenant_slug)
    if not profile.get("contract_start_ts"):
        return
    due_day        = int(profile.get("due_day") or 10)
    default_amount = float(profile.get("plan_amount") or 0.0)
    start_period   = _period_from_ts(float(profile.get("contract_start_ts") or time.time()))
    invoices       = list_invoices(tenant_slug)
    all_periods    = sorted([i["period_ym"] for i in invoices if i.get("period_ym")])
    open_count     = len([i for i in invoices if not i.get("paid")])

    next_period = _next_period(all_periods[-1]) if all_periods else start_period

    while open_count < 3:
        upsert_invoice(
            tenant_slug,
            {
                "period_ym": next_period,
                "due_ts":    _due_ts_for_period(next_period, due_day),
                "amount":    default_amount,
                "paid":      False,
                "notes":     "Fatura gerada automaticamente",
            },
        )
        open_count  += 1
        next_period  = _next_period(next_period)


def get_financial_summary(tenant_slug: str, now_ts: float | None = None) -> dict:
    now_ts   = now_ts or time.time()
    invoices = list_invoices(tenant_slug)
    overdue  = [i for i in invoices if not i.get("paid") and (i.get("due_ts") or 0) > 0 and i["due_ts"] < now_ts]
    open_items = [i for i in invoices if not i.get("paid")]
    return {
        "invoice_count":  len(invoices),
        "open_count":     len(open_items),
        "overdue_count":  len(overdue),
        "overdue_amount": float(sum(float(i.get("amount") or 0.0) for i in overdue)),
        "status":         "atrasado" if overdue else ("pendente" if open_items else "em_dia"),
    }
