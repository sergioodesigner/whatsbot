"""Supabase Postgres repository for execution tracking tables (Phase 4).

Drop-in equivalent of ``db.repositories.execution_repo`` backed by Postgres.
"""

import json
import time

from db import tenant_pg_connection as pg


def create(phone: str, trigger_type: str = "webhook") -> int:
    """Create a new execution and return its ID."""
    slug = pg._get_slug()
    row = pg.execute_returning(
        "INSERT INTO executions (tenant_slug, phone, trigger_type, started_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (slug, phone, trigger_type, time.time()),
    )
    return row["id"] if row else 0


def add_step(execution_id: int, step_type: str,
             data: dict | None = None, status: str = "ok") -> int:
    """Add a step to an execution and return step ID."""
    slug = pg._get_slug()
    data_json = json.dumps(data, ensure_ascii=False) if data else None
    row = pg.execute_returning(
        "INSERT INTO execution_steps (tenant_slug, execution_id, step_type, status, data, ts) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (slug, execution_id, step_type, status, data_json, time.time()),
    )
    return row["id"] if row else 0


def complete(execution_id: int, status: str = "completed",
             error: str | None = None) -> None:
    """Mark an execution as completed or failed."""
    slug = pg._get_slug()
    pg.execute(
        "UPDATE executions SET status = %s, completed_at = %s, error = %s WHERE id = %s AND tenant_slug = %s",
        (status, time.time(), error, execution_id, slug),
    )


def get_by_id(execution_id: int) -> dict | None:
    """Return an execution with all its steps."""
    slug = pg._get_slug()
    row = pg.fetchone(
        "SELECT * FROM executions WHERE id = %s AND tenant_slug = %s", (execution_id, slug)
    )
    if not row:
        return None

    execution = dict(row)
    steps = pg.fetchall(
        "SELECT * FROM execution_steps WHERE execution_id = %s AND tenant_slug = %s ORDER BY ts",
        (execution_id, slug),
    )
    execution["steps"] = []
    for s in steps:
        step = dict(s)
        if step.get("data"):
            try:
                step["data"] = json.loads(step["data"])
            except (json.JSONDecodeError, TypeError):
                pass
        execution["steps"].append(step)
    return execution


def list_executions(limit: int = 50, offset: int = 0,
                    phone: str | None = None,
                    status: str | None = None) -> list[dict]:
    """List executions (newest first) with step count and duration."""
    slug = pg._get_slug()
    clauses = ["e.tenant_slug = %s"]
    params: list = [slug]
    
    if phone:
        clauses.append("e.phone = %s")
        params.append(phone)
    if status:
        clauses.append("e.status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses)

    rows = pg.fetchall(
        f"""SELECT e.*,
                   (SELECT COUNT(*) FROM execution_steps s WHERE s.execution_id = e.id AND s.tenant_slug = %s) AS step_count
            FROM executions e {where}
            ORDER BY e.id DESC
            LIMIT %s OFFSET %s""",
        [slug] + params + [limit, offset],
    )

    results = []
    for r in rows:
        d = dict(r)
        if d.get("started_at") and d.get("completed_at"):
            d["duration_ms"] = round((d["completed_at"] - d["started_at"]) * 1000)
        else:
            d["duration_ms"] = None
        results.append(d)
    return results


def count(phone: str | None = None, status: str | None = None) -> int:
    """Count total executions for pagination."""
    slug = pg._get_slug()
    clauses = ["tenant_slug = %s"]
    params: list = [slug]
    if phone:
        clauses.append("phone = %s")
        params.append(phone)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses)
    row = pg.fetchone(f"SELECT COUNT(*) AS cnt FROM executions {where}", params)
    return row["cnt"] if row else 0


def prune(max_keep: int) -> int:
    """Delete oldest executions keeping only the most recent max_keep. Returns count deleted."""
    slug = pg._get_slug()
    with pg.dict_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS cnt FROM executions WHERE tenant_slug = %s", (slug,))
        row = cur.fetchone()
        total = row["cnt"] if row else 0
        if total <= max_keep:
            return 0
        cur.execute(
            """DELETE FROM executions WHERE id NOT IN (
                SELECT id FROM executions WHERE tenant_slug = %s ORDER BY id DESC LIMIT %s
            ) AND tenant_slug = %s""",
            (slug, max_keep, slug),
        )
        count = cur.rowcount
        conn.commit()
    return count


def get_webhook_payloads(limit: int = 50) -> list[dict]:
    """Get recent webhook payloads from execution steps."""
    slug = pg._get_slug()
    rows = pg.fetchall(
        """SELECT s.ts, s.data, e.phone
           FROM execution_steps s
           JOIN executions e ON e.id = s.execution_id AND e.tenant_slug = %s
           WHERE s.step_type = 'webhook_received' AND s.tenant_slug = %s
           ORDER BY s.ts DESC
           LIMIT %s""",
        (slug, slug, limit),
    )
    results = []
    for r in rows:
        entry = {"ts": r["ts"], "phone": r["phone"]}
        if r["data"]:
            try:
                entry["payload"] = json.loads(r["data"])
            except (json.JSONDecodeError, TypeError):
                entry["payload"] = r["data"]
        else:
            entry["payload"] = {}
        results.append(entry)
    results.reverse()
    return results
