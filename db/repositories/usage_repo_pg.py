"""Supabase Postgres repository for usage (cost tracking) table (Phase 4).

Drop-in equivalent of ``db.repositories.usage_repo`` backed by Postgres.
"""

import time

from db import tenant_pg_connection as pg


def add(contact_id: int, call_type: str, model: str,
        prompt_tokens: int, completion_tokens: int,
        total_tokens: int, cost_usd: float) -> None:
    """Insert a usage record."""
    slug = pg._get_slug()
    pg.execute(
        """INSERT INTO usage (tenant_slug, contact_id, call_type, model, prompt_tokens,
           completion_tokens, total_tokens, cost_usd, ts)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (slug, contact_id, call_type, model, prompt_tokens,
         completion_tokens, total_tokens, cost_usd, time.time()),
    )


def _time_filter(start_ts: float | None, end_ts: float | None) -> tuple[str, list]:
    """Build WHERE clause fragment for time filtering."""
    clauses = []
    params = []
    if start_ts is not None:
        clauses.append("ts >= %s")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("ts <= %s")
        params.append(end_ts)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def summary(contact_id: int, start_ts: float | None = None,
            end_ts: float | None = None) -> dict:
    """Return aggregated usage stats for a single contact."""
    time_where, time_params = _time_filter(start_ts, end_ts)
    slug = pg._get_slug()

    # Overall totals
    row = pg.fetchone(
        f"""SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COUNT(*) AS call_count
            FROM usage WHERE contact_id = %s AND tenant_slug = %s{time_where}""",
        [contact_id, slug] + time_params,
    )

    totals = {
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "cost_usd": row["cost_usd"],
        "call_count": row["call_count"],
        "by_type": {},
    }

    # Breakdown by call_type
    by_type_rows = pg.fetchall(
        f"""SELECT call_type,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COUNT(*) AS call_count
            FROM usage WHERE contact_id = %s AND tenant_slug = %s{time_where}
            GROUP BY call_type""",
        [contact_id, slug] + time_params,
    )

    for r in by_type_rows:
        totals["by_type"][r["call_type"]] = {
            "cost_usd": r["cost_usd"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "total_tokens": r["total_tokens"],
            "call_count": r["call_count"],
        }

    return totals


def global_summary(start_ts: float | None = None,
                   end_ts: float | None = None) -> dict:
    """Return aggregated usage stats across ALL contacts."""
    time_where, time_params = _time_filter(start_ts, end_ts)
    slug = pg._get_slug()

    row = pg.fetchone(
        f"""SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COUNT(*) AS call_count
            FROM usage WHERE tenant_slug = %s{time_where}""",
        [slug] + time_params,
    )

    totals = {
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "total_tokens": row["total_tokens"],
        "cost_usd": row["cost_usd"],
        "call_count": row["call_count"],
        "by_type": {},
    }

    by_type_rows = pg.fetchall(
        f"""SELECT call_type,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                   COUNT(*) AS call_count
            FROM usage WHERE tenant_slug = %s{time_where}
            GROUP BY call_type""",
        [slug] + time_params,
    )

    for r in by_type_rows:
        totals["by_type"][r["call_type"]] = {
            "cost_usd": r["cost_usd"],
            "prompt_tokens": r["prompt_tokens"],
            "completion_tokens": r["completion_tokens"],
            "total_tokens": r["total_tokens"],
            "call_count": r["call_count"],
        }

    return totals


def by_contact(start_ts: float | None = None,
               end_ts: float | None = None) -> list[dict]:
    """Return usage breakdown per contact (for the by-contact endpoint)."""
    time_where, time_params = _time_filter(start_ts, end_ts)
    slug = pg._get_slug()

    rows = pg.fetchall(
        f"""SELECT u.contact_id, c.phone, c.name,
                   COALESCE(SUM(u.prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(u.completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(u.total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(u.cost_usd), 0.0) AS cost_usd,
                   COUNT(*) AS call_count
            FROM usage u
            JOIN contacts c ON c.id = u.contact_id AND c.tenant_slug = %s
            WHERE u.tenant_slug = %s{time_where}
            GROUP BY u.contact_id, c.phone, c.name
            HAVING COUNT(*) > 0
            ORDER BY cost_usd DESC""",
        [slug, slug] + time_params,
    )

    results = []
    with pg.get_pg_conn() as conn:
        with conn.cursor() as cur:
            for row in rows:
                contact_id = row["contact_id"]
                cur.execute(
                    f"""SELECT call_type,
                               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                               COALESCE(SUM(total_tokens), 0) AS total_tokens,
                               COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                               COUNT(*) AS call_count
                        FROM usage WHERE contact_id = %s AND tenant_slug = %s{time_where}
                        GROUP BY call_type""",
                    [contact_id, slug] + time_params,
                )
                by_type_rows = cur.fetchall()
                
                by_type = {}
                for r in by_type_rows:
                    by_type[r[0]] = {
                        "prompt_tokens": r[1],
                        "completion_tokens": r[2],
                        "total_tokens": r[3],
                        "cost_usd": r[4],
                        "call_count": r[5],
                    }

                results.append({
                    "phone": row["phone"],
                    "name": row["name"] or "",
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                    "cost_usd": row["cost_usd"],
                    "call_count": row["call_count"],
                    "by_type": by_type,
                })

    return results


def detail(contact_id: int, start_ts: float | None = None,
           end_ts: float | None = None) -> list[dict]:
    """Return raw usage records for a specific contact."""
    time_where, time_params = _time_filter(start_ts, end_ts)
    slug = pg._get_slug()
    rows = pg.fetchall(
        f"""SELECT call_type, model, prompt_tokens, completion_tokens,
                   total_tokens, cost_usd, ts
            FROM usage WHERE contact_id = %s AND tenant_slug = %s{time_where}
            ORDER BY ts""",
        [contact_id, slug] + time_params,
    )
    return [dict(r) for r in rows]
