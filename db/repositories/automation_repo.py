"""Repository for tenant-scoped CRM automations."""

import json
import ipaddress
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from db.connection import get_db
from db.repositories import config_repo, crm_repo, tag_repo

_SCHEMA_READY = False
MAX_CHAIN_ACTIONS_PER_DEAL = 5
FINGERPRINT_WINDOW_SECONDS = 60.0


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_rules (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            enabled       INTEGER NOT NULL DEFAULT 1,
            trigger_type  TEXT    NOT NULL,
            from_stage    TEXT    NOT NULL DEFAULT '',
            to_stage      TEXT    NOT NULL DEFAULT '',
            condition_owner TEXT  NOT NULL DEFAULT '',
            condition_min_value REAL,
            condition_tag  TEXT   NOT NULL DEFAULT '',
            action_type   TEXT    NOT NULL,
            action_payload TEXT   NOT NULL DEFAULT '{}',
            created_at    REAL    NOT NULL,
            updated_at    REAL    NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id       INTEGER REFERENCES automation_rules(id) ON DELETE SET NULL,
            deal_id       INTEGER,
            fingerprint   TEXT    NOT NULL DEFAULT '',
            trigger_type  TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'ok',
            context       TEXT    NOT NULL DEFAULT '{}',
            result        TEXT    NOT NULL DEFAULT '{}',
            error         TEXT    NOT NULL DEFAULT '',
            ts            REAL    NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_rules_trigger ON automation_rules(trigger_type, enabled)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_runs_ts ON automation_runs(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_runs_fingerprint_ts ON automation_runs(fingerprint, ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_automation_runs_deal_ts ON automation_runs(deal_id, ts)"
    )
    cols = conn.execute("PRAGMA table_info(automation_rules)").fetchall()
    col_names = {c["name"] for c in cols}
    if "condition_owner" not in col_names:
        conn.execute("ALTER TABLE automation_rules ADD COLUMN condition_owner TEXT NOT NULL DEFAULT ''")
    if "condition_min_value" not in col_names:
        conn.execute("ALTER TABLE automation_rules ADD COLUMN condition_min_value REAL")
    if "condition_tag" not in col_names:
        conn.execute("ALTER TABLE automation_rules ADD COLUMN condition_tag TEXT NOT NULL DEFAULT ''")

    run_cols = conn.execute("PRAGMA table_info(automation_runs)").fetchall()
    run_col_names = {c["name"] for c in run_cols}
    if "deal_id" not in run_col_names:
        conn.execute("ALTER TABLE automation_runs ADD COLUMN deal_id INTEGER")
    if "fingerprint" not in run_col_names:
        conn.execute("ALTER TABLE automation_runs ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
    conn.commit()
    _SCHEMA_READY = True


def _parse_json(value: str, default):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _normalize_rule(row: dict) -> dict:
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item["action_payload"] = _parse_json(item.get("action_payload", "{}"), {})
    item["conditions"] = {
        "owner": str(item.get("condition_owner") or "").strip(),
        "min_value": item.get("condition_min_value"),
        "contact_tag": str(item.get("condition_tag") or "").strip(),
    }
    return item


def _record_run(
    rule_id: int | None,
    deal_id: int | None,
    fingerprint: str,
    trigger_type: str,
    status: str,
    context: dict | None = None,
    result: dict | None = None,
    error: str = "",
) -> None:
    conn = get_db()
    conn.execute(
        """
        INSERT INTO automation_runs (rule_id, deal_id, fingerprint, trigger_type, status, context, result, error, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            deal_id,
            str(fingerprint or ""),
            trigger_type,
            status,
            json.dumps(context or {}, ensure_ascii=False),
            json.dumps(result or {}, ensure_ascii=False),
            str(error or ""),
            time.time(),
        ),
    )
    conn.commit()


def list_rules() -> list[dict]:
    _ensure_schema()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, name, enabled, trigger_type, from_stage, to_stage, action_type, action_payload, created_at, updated_at
             , condition_owner, condition_min_value, condition_tag
        FROM automation_rules
        ORDER BY id DESC
        """
    ).fetchall()
    return [_normalize_rule(r) for r in rows]


def create_rule(data: dict) -> dict:
    _ensure_schema()
    now = time.time()
    name = str(data.get("name", "")).strip()
    trigger_type = str(data.get("trigger_type", "deal_stage_changed")).strip()
    from_stage = str(data.get("from_stage", "")).strip()
    to_stage = str(data.get("to_stage", "")).strip()
    action_type = str(data.get("action_type", "")).strip()
    action_payload = data.get("action_payload") or {}
    conditions = data.get("conditions") or {}
    condition_owner = str(conditions.get("owner", data.get("condition_owner", "")) or "").strip()
    condition_tag = str(conditions.get("contact_tag", data.get("condition_tag", "")) or "").strip()
    min_value_raw = conditions.get("min_value", data.get("condition_min_value"))
    condition_min_value = None
    if min_value_raw not in (None, "", False):
        condition_min_value = float(min_value_raw)
    enabled = 1 if bool(data.get("enabled", True)) else 0

    if not name:
        raise ValueError("name é obrigatório.")
    if trigger_type != "deal_stage_changed":
        raise ValueError("trigger_type inválido.")
    if action_type not in ("create_task", "move_stage", "webhook"):
        raise ValueError("action_type inválido.")
    if from_stage and from_stage not in crm_repo.DEFAULT_STAGES:
        raise ValueError("from_stage inválido.")
    if to_stage and to_stage not in crm_repo.DEFAULT_STAGES:
        raise ValueError("to_stage inválido.")

    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO automation_rules (
            name, enabled, trigger_type, from_stage, to_stage,
            condition_owner, condition_min_value, condition_tag,
            action_type, action_payload, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            enabled,
            trigger_type,
            from_stage,
            to_stage,
            condition_owner,
            condition_min_value,
            condition_tag,
            action_type,
            json.dumps(action_payload, ensure_ascii=False),
            now,
            now,
        ),
    )
    conn.commit()
    rule_id = int(cur.lastrowid)
    return get_rule(rule_id) or {}


def get_rule(rule_id: int) -> dict | None:
    _ensure_schema()
    conn = get_db()
    row = conn.execute(
        """
        SELECT id, name, enabled, trigger_type, from_stage, to_stage, action_type, action_payload, created_at, updated_at
             , condition_owner, condition_min_value, condition_tag
        FROM automation_rules
        WHERE id = ?
        """,
        (rule_id,),
    ).fetchone()
    return _normalize_rule(row) if row else None


def update_rule(rule_id: int, data: dict) -> dict | None:
    _ensure_schema()
    current = get_rule(rule_id)
    if not current:
        return None
    conditions_input = data.get("conditions") or {}
    payload = {
        "name": str(data.get("name", current["name"])).strip(),
        "enabled": 1 if bool(data.get("enabled", current["enabled"])) else 0,
        "trigger_type": str(data.get("trigger_type", current["trigger_type"])).strip(),
        "from_stage": str(data.get("from_stage", current["from_stage"])).strip(),
        "to_stage": str(data.get("to_stage", current["to_stage"])).strip(),
        "condition_owner": str(
            conditions_input.get("owner", data.get("condition_owner", current.get("condition_owner", ""))) or ""
        ).strip(),
        "condition_tag": str(
            conditions_input.get("contact_tag", data.get("condition_tag", current.get("condition_tag", ""))) or ""
        ).strip(),
        "condition_min_value": conditions_input.get(
            "min_value", data.get("condition_min_value", current.get("condition_min_value"))
        ),
        "action_type": str(data.get("action_type", current["action_type"])).strip(),
        "action_payload": data.get("action_payload", current["action_payload"]),
    }
    if not payload["name"]:
        raise ValueError("name é obrigatório.")
    if payload["trigger_type"] != "deal_stage_changed":
        raise ValueError("trigger_type inválido.")
    if payload["action_type"] not in ("create_task", "move_stage", "webhook"):
        raise ValueError("action_type inválido.")
    if payload["from_stage"] and payload["from_stage"] not in crm_repo.DEFAULT_STAGES:
        raise ValueError("from_stage inválido.")
    if payload["to_stage"] and payload["to_stage"] not in crm_repo.DEFAULT_STAGES:
        raise ValueError("to_stage inválido.")

    if payload["condition_min_value"] in ("", False):
        payload["condition_min_value"] = None
    if payload["condition_min_value"] is not None:
        payload["condition_min_value"] = float(payload["condition_min_value"])

    conn = get_db()
    conn.execute(
        """
        UPDATE automation_rules
        SET name = ?, enabled = ?, trigger_type = ?, from_stage = ?, to_stage = ?, condition_owner = ?, condition_min_value = ?, condition_tag = ?, action_type = ?, action_payload = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            payload["name"],
            payload["enabled"],
            payload["trigger_type"],
            payload["from_stage"],
            payload["to_stage"],
            payload["condition_owner"],
            payload["condition_min_value"],
            payload["condition_tag"],
            payload["action_type"],
            json.dumps(payload["action_payload"], ensure_ascii=False),
            time.time(),
            rule_id,
        ),
    )
    conn.commit()
    return get_rule(rule_id)


def delete_rule(rule_id: int) -> bool:
    _ensure_schema()
    conn = get_db()
    cur = conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def list_runs(limit: int = 100) -> list[dict]:
    _ensure_schema()
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, rule_id, deal_id, fingerprint, trigger_type, status, context, result, error, ts
        FROM automation_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["context"] = _parse_json(item.get("context", "{}"), {})
        item["result"] = _parse_json(item.get("result", "{}"), {})
        items.append(item)
    return items


def apply_deal_stage_changed(before: dict, after: dict) -> int:
    _ensure_schema()
    from_stage = str(before.get("stage", "")).strip()
    to_stage = str(after.get("stage", "")).strip()
    if not from_stage or not to_stage or from_stage == to_stage:
        return 0

    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, name, enabled, trigger_type, from_stage, to_stage, action_type, action_payload, created_at, updated_at
             , condition_owner, condition_min_value, condition_tag
        FROM automation_rules
        WHERE trigger_type = 'deal_stage_changed' AND enabled = 1
        ORDER BY id ASC
        """
    ).fetchall()
    executed = 0
    deal_id = int(after.get("id") or 0)
    if deal_id <= 0:
        return 0
    contact_tags = _get_contact_tags(after)
    template_ctx = _build_template_context(before, after, contact_tags)
    chain_actions = 0

    for row in rows:
        rule = _normalize_rule(row)
        if rule["from_stage"] and rule["from_stage"] != from_stage:
            continue
        if rule["to_stage"] and rule["to_stage"] != to_stage:
            continue
        if not _rule_matches_conditions(rule, after, contact_tags):
            continue
        if chain_actions >= MAX_CHAIN_ACTIONS_PER_DEAL:
            _record_run(
                rule_id=rule["id"],
                deal_id=deal_id,
                fingerprint=f"blocked:max_chain:{rule['id']}:{deal_id}",
                trigger_type="deal_stage_changed",
                status="skipped",
                context={
                    "deal_id": deal_id,
                    "from_stage": from_stage,
                    "to_stage": to_stage,
                },
                result={"reason": "max_chain_actions_reached", "max": MAX_CHAIN_ACTIONS_PER_DEAL},
            )
            break

        fingerprint = _build_fingerprint(rule, deal_id, from_stage, to_stage)
        if _was_fingerprint_recent(fingerprint, FINGERPRINT_WINDOW_SECONDS):
            _record_run(
                rule_id=rule["id"],
                deal_id=deal_id,
                fingerprint=fingerprint,
                trigger_type="deal_stage_changed",
                status="skipped",
                context={
                    "deal_id": deal_id,
                    "from_stage": from_stage,
                    "to_stage": to_stage,
                },
                result={"reason": "deduplicated_recent_run", "window_seconds": FINGERPRINT_WINDOW_SECONDS},
            )
            continue

        context = {
            "deal_id": deal_id,
            "contact_phone": after.get("contact_phone"),
            "from_stage": from_stage,
            "to_stage": to_stage,
            "contact_tags": contact_tags,
        }
        try:
            if rule["action_type"] == "create_task":
                title_template = str(rule["action_payload"].get("title_template", "")).strip()
                notes_template = str(rule["action_payload"].get("notes_template", "")).strip()
                task_title = _render_template(
                    title_template or str(rule["action_payload"].get("title", "")).strip() or "Follow-up automático",
                    template_ctx,
                )
                task_notes = _render_template(
                    notes_template or str(rule["action_payload"].get("notes", "")).strip(),
                    template_ctx,
                )
                crm_repo.create_task(
                    int(after["id"]),
                    {"title": task_title, "notes": task_notes},
                )
                _record_run(
                    rule_id=rule["id"],
                    deal_id=deal_id,
                    fingerprint=fingerprint,
                    trigger_type="deal_stage_changed",
                    status="ok",
                    context=context,
                    result={"action": "create_task", "title": task_title},
                )
            elif rule["action_type"] == "move_stage":
                next_stage = str(rule["action_payload"].get("to_stage", "")).strip()
                if not next_stage or next_stage not in crm_repo.DEFAULT_STAGES:
                    raise ValueError("to_stage da ação é inválido.")
                if next_stage == str(after.get("stage") or "").strip():
                    _record_run(
                        rule_id=rule["id"],
                        deal_id=deal_id,
                        fingerprint=fingerprint,
                        trigger_type="deal_stage_changed",
                        status="skipped",
                        context=context,
                        result={"reason": "no_op_stage", "stage": next_stage},
                    )
                    continue
                conn.execute(
                    "UPDATE crm_deals SET stage = ?, updated_at = ? WHERE id = ?",
                    (next_stage, time.time(), int(after["id"])),
                )
                conn.commit()
                _record_run(
                    rule_id=rule["id"],
                    deal_id=deal_id,
                    fingerprint=fingerprint,
                    trigger_type="deal_stage_changed",
                    status="ok",
                    context=context,
                    result={"action": "move_stage", "to_stage": next_stage},
                )
            elif rule["action_type"] == "webhook":
                webhook_result = _trigger_webhook(rule, template_ctx)
                _record_run(
                    rule_id=rule["id"],
                    deal_id=deal_id,
                    fingerprint=fingerprint,
                    trigger_type="deal_stage_changed",
                    status="ok",
                    context=context,
                    result={"action": "webhook", **webhook_result},
                )
            executed += 1
            chain_actions += 1
        except Exception as exc:
            _record_run(
                rule_id=rule["id"],
                deal_id=deal_id,
                fingerprint=fingerprint,
                trigger_type="deal_stage_changed",
                status="error",
                context=context,
                error=str(exc),
            )

    return executed


def simulate_rule(rule_id: int, deal_id: int, from_stage: str = "") -> dict:
    _ensure_schema()
    rule = get_rule(rule_id)
    if not rule:
        raise ValueError("Regra não encontrada.")
    deal = crm_repo.get_deal(deal_id)
    if not deal:
        raise ValueError("Oportunidade não encontrada.")

    candidate_from = str(from_stage or "").strip() or str(rule.get("from_stage") or "").strip()
    if not candidate_from:
        candidate_from = str(deal.get("stage") or "").strip()
    candidate_to = str(deal.get("stage") or "").strip()
    contact_tags = _get_contact_tags(deal)
    template_ctx = _build_template_context({"stage": candidate_from}, deal, contact_tags)

    stage_from_ok = (not rule.get("from_stage")) or (rule.get("from_stage") == candidate_from)
    stage_to_ok = (not rule.get("to_stage")) or (rule.get("to_stage") == candidate_to)
    conditions_ok = _rule_matches_conditions(rule, deal, contact_tags)
    will_run = bool(rule.get("enabled")) and stage_from_ok and stage_to_ok and conditions_ok

    action_preview = {"action_type": rule.get("action_type")}
    if rule.get("action_type") == "create_task":
        payload = rule.get("action_payload") or {}
        action_preview["title"] = _render_template(
            str(payload.get("title_template", "")).strip() or str(payload.get("title", "")).strip() or "Follow-up automático",
            template_ctx,
        )
        action_preview["notes"] = _render_template(
            str(payload.get("notes_template", "")).strip() or str(payload.get("notes", "")).strip(),
            template_ctx,
        )
    elif rule.get("action_type") == "move_stage":
        action_preview["to_stage"] = str((rule.get("action_payload") or {}).get("to_stage", "")).strip()
    elif rule.get("action_type") == "webhook":
        req = _prepare_webhook_request(rule, template_ctx)
        security = _get_webhook_security_settings()
        try:
            _enforce_webhook_url_security(req.get("url", ""), security)
            req["security_ok"] = True
            req["security_error"] = ""
        except Exception as exc:
            req["security_ok"] = False
            req["security_error"] = str(exc)
        action_preview["webhook"] = req

    return {
        "rule_id": rule_id,
        "deal_id": deal_id,
        "enabled": bool(rule.get("enabled")),
        "stage_match": {"from_ok": stage_from_ok, "to_ok": stage_to_ok},
        "conditions_match": conditions_ok,
        "will_run": will_run,
        "from_stage_input": candidate_from,
        "to_stage_input": candidate_to,
        "contact_tags": contact_tags,
        "action_preview": action_preview,
        "context_preview": template_ctx,
    }


def _get_contact_tags(deal: dict) -> list[str]:
    contact_id = deal.get("contact_id")
    if contact_id:
        try:
            return tag_repo.get_contact_tags(int(contact_id))
        except Exception:
            return []
    return []


def _rule_matches_conditions(rule: dict, deal: dict, contact_tags: list[str]) -> bool:
    conditions = rule.get("conditions") or {}
    owner_filter = str(conditions.get("owner") or "").strip().lower()
    min_value = conditions.get("min_value")
    tag_filter = str(conditions.get("contact_tag") or "").strip().lower()

    if owner_filter:
        owner = str(deal.get("owner") or "").strip().lower()
        if owner_filter not in owner:
            return False
    if min_value not in (None, ""):
        try:
            if float(deal.get("potential_value") or 0.0) < float(min_value):
                return False
        except (TypeError, ValueError):
            return False
    if tag_filter:
        normalized_tags = {str(t).strip().lower() for t in contact_tags}
        if tag_filter not in normalized_tags:
            return False
    return True


def _build_template_context(before: dict, after: dict, contact_tags: list[str]) -> dict[str, str]:
    now_dt = datetime.now(timezone.utc)
    return {
        "deal.id": str(after.get("id") or ""),
        "deal.title": str(after.get("title") or ""),
        "deal.stage": str(after.get("stage") or ""),
        "deal.from_stage": str(before.get("stage") or ""),
        "deal.to_stage": str(after.get("stage") or ""),
        "deal.owner": str(after.get("owner") or ""),
        "deal.potential_value": str(after.get("potential_value") or 0),
        "contact.id": str(after.get("contact_id") or ""),
        "contact.phone": str(after.get("contact_phone") or ""),
        "contact.name": str((after.get("contact") or {}).get("name") or ""),
        "contact.company": str((after.get("contact") or {}).get("company") or ""),
        "contact.email": str((after.get("contact") or {}).get("email") or ""),
        "contact.tags_csv": ",".join(contact_tags),
        "contact.tags_json": json.dumps(contact_tags, ensure_ascii=False),
        "now_iso": now_dt.isoformat(),
        "now_ts": str(time.time()),
    }


def _render_template(template: str, context: dict[str, str]) -> str:
    text = str(template or "")
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text.strip()


def _trigger_webhook(rule: dict, template_ctx: dict[str, str]) -> dict:
    req = _prepare_webhook_request(rule, template_ctx)
    url = req["url"]
    method = req["method"]
    rendered_headers = req["headers"]
    rendered_body = req.get("body", "")
    timeout_seconds = req["timeout_seconds"]
    security = _get_webhook_security_settings()
    if not url:
        raise ValueError("Webhook sem URL.")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError("Webhook method inválido.")
    _enforce_webhook_url_security(url, security)

    max_retries = security["max_retries"]
    retry_backoff = security["retry_backoff_seconds"]
    attempts = max_retries + 1
    last_error: str | None = None
    response = None
    used_attempts = 0

    for attempt in range(attempts):
        used_attempts = attempt + 1
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                if rendered_body and method in {"POST", "PUT", "PATCH", "DELETE"}:
                    try:
                        json_body = json.loads(rendered_body)
                        response = client.request(method, url, headers=rendered_headers, json=json_body)
                    except (json.JSONDecodeError, TypeError):
                        response = client.request(method, url, headers=rendered_headers, content=rendered_body.encode("utf-8"))
                else:
                    response = client.request(method, url, headers=rendered_headers)
            # Retry only for transient server/network classes
            if response.status_code >= 500 and attempt < max_retries:
                time.sleep(retry_backoff * (attempt + 1))
                continue
            break
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries:
                time.sleep(retry_backoff * (attempt + 1))
                continue
            raise ValueError(f"Falha ao enviar webhook: {exc}") from exc

    if response is None:
        raise ValueError(last_error or "Falha desconhecida no webhook.")

    return {
        "url": url,
        "method": method,
        "status_code": response.status_code,
        "ok": 200 <= response.status_code < 300,
        "attempts": used_attempts,
    }


def _prepare_webhook_request(rule: dict, template_ctx: dict[str, str]) -> dict:
    payload = rule.get("action_payload") or {}
    headers = payload.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}
    return {
        "url": str(payload.get("url", "")).strip(),
        "method": str(payload.get("method", "POST")).strip().upper() or "POST",
        "headers": {str(k): _render_template(str(v), template_ctx) for k, v in headers.items()},
        "body": _render_template(str(payload.get("body_template", "") or "").strip(), template_ctx),
        "timeout_seconds": max(1.0, min(float(payload.get("timeout_seconds") or 10), 30.0)),
    }


def _get_webhook_security_settings() -> dict:
    allowed_domains = config_repo.get("automation_webhook_allowed_domains", [])
    if not isinstance(allowed_domains, list):
        allowed_domains = []
    normalized_domains = []
    for item in allowed_domains:
        host = str(item or "").strip().lower()
        if host:
            normalized_domains.append(host.lstrip("."))
    return {
        "allowed_domains": normalized_domains,
        "allow_http": bool(config_repo.get("automation_webhook_allow_http", False)),
        "block_private_hosts": bool(config_repo.get("automation_webhook_block_private_hosts", True)),
        "max_retries": max(0, min(int(config_repo.get("automation_webhook_max_retries", 1) or 1), 5)),
        "retry_backoff_seconds": max(
            0.1, min(float(config_repo.get("automation_webhook_retry_backoff_seconds", 0.8) or 0.8), 10.0)
        ),
    }


def _enforce_webhook_url_security(url: str, security: dict) -> None:
    parsed = urlparse(str(url or "").strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Webhook URL inválida: esquema deve ser http ou https.")
    if not host:
        raise ValueError("Webhook URL inválida: host ausente.")
    if scheme != "https" and not security.get("allow_http", False):
        raise ValueError("Webhook inseguro bloqueado: use HTTPS.")

    allowed_domains = security.get("allowed_domains") or []
    if allowed_domains and not _host_matches_whitelist(host, allowed_domains):
        raise ValueError("Webhook bloqueado: domínio fora da whitelist.")

    if security.get("block_private_hosts", True) and _is_private_or_local_host(host):
        raise ValueError("Webhook bloqueado: host local/privado não permitido.")


def _host_matches_whitelist(host: str, domains: list[str]) -> bool:
    for domain in domains:
        d = str(domain or "").strip().lower().lstrip(".")
        if not d:
            continue
        if host == d or host.endswith("." + d):
            return True
    return False


def _is_private_or_local_host(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass
    # Resolve DNS host and inspect resulting addresses.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return True
        except ValueError:
            continue
    return False


def _build_fingerprint(rule: dict, deal_id: int, from_stage: str, to_stage: str) -> str:
    action_type = str(rule.get("action_type") or "").strip()
    payload = rule.get("action_payload") or {}
    action_key = ""
    if action_type == "move_stage":
        action_key = str(payload.get("to_stage", "")).strip()
    elif action_type == "create_task":
        action_key = str(payload.get("title_template") or payload.get("title") or "").strip()
    elif action_type == "webhook":
        action_key = f"{str(payload.get('method', 'POST')).upper()}:{str(payload.get('url', '')).strip()}"
    return f"{rule.get('id')}|{deal_id}|{from_stage}|{to_stage}|{action_type}|{action_key}"


def _was_fingerprint_recent(fingerprint: str, window_seconds: float) -> bool:
    conn = get_db()
    cutoff = time.time() - float(window_seconds)
    row = conn.execute(
        """
        SELECT 1
        FROM automation_runs
        WHERE fingerprint = ?
          AND ts >= ?
          AND status IN ('ok', 'skipped')
        LIMIT 1
        """,
        (str(fingerprint or ""), cutoff),
    ).fetchone()
    return row is not None
