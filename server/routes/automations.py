"""Automation rules endpoints for CRM workflows."""

import asyncio

from db.repositories import automation_repo
from server.helpers import _ok, _err


def register_routes(app, deps):
    @app.get("/api/automations/rules")
    async def list_automation_rules():
        items = await asyncio.to_thread(automation_repo.list_rules)
        return _ok({"items": items})

    @app.post("/api/automations/rules")
    async def create_automation_rule(body: dict):
        try:
            item = await asyncio.to_thread(automation_repo.create_rule, body or {})
        except ValueError as exc:
            return _err(str(exc), status=400)
        return _ok({"item": item})

    @app.put("/api/automations/rules/{rule_id}")
    async def update_automation_rule(rule_id: int, body: dict):
        try:
            item = await asyncio.to_thread(automation_repo.update_rule, rule_id, body or {})
        except ValueError as exc:
            return _err(str(exc), status=400)
        if not item:
            return _err("Regra não encontrada.", status=404)
        return _ok({"item": item})

    @app.delete("/api/automations/rules/{rule_id}")
    async def delete_automation_rule(rule_id: int):
        deleted = await asyncio.to_thread(automation_repo.delete_rule, rule_id)
        if not deleted:
            return _err("Regra não encontrada.", status=404)
        return _ok({"message": "Regra excluída."})

    @app.post("/api/automations/rules/{rule_id}/simulate")
    async def simulate_automation_rule(rule_id: int, body: dict | None = None):
        payload = body or {}
        try:
            deal_id = int(payload.get("deal_id"))
        except (TypeError, ValueError):
            return _err("deal_id é obrigatório.", status=400)
        try:
            result = await asyncio.to_thread(
                automation_repo.simulate_rule,
                rule_id,
                deal_id,
                str(payload.get("from_stage", "") or ""),
            )
        except ValueError as exc:
            return _err(str(exc), status=400)
        return _ok({"result": result})

    @app.get("/api/automations/runs")
    async def list_automation_runs(limit: int = 100):
        items = await asyncio.to_thread(automation_repo.list_runs, limit)
        return _ok({"items": items})
