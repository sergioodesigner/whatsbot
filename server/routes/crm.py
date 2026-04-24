"""CRM endpoints (simple funnel + tasks), integrated with contacts."""

import asyncio

from db.repositories import crm_repo, master_policy_repo
from server.helpers import _ok, _err
from server.tenant import current_tenant_slug


def register_routes(app, deps):
    def _crm_enabled() -> bool:
        slug = current_tenant_slug.get()
        if not slug or slug in ("default", "__superadmin__"):
            return True
        try:
            return bool(master_policy_repo.get_tenant(slug, "crm_enabled", True))
        except RuntimeError:
            return True

    @app.get("/api/crm/board")
    async def crm_board():
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)

        deals = await asyncio.to_thread(crm_repo.list_deals)
        grouped = {stage: [] for stage in crm_repo.DEFAULT_STAGES}
        for d in deals:
            grouped.setdefault(d.get("stage") or "novo", []).append(d)
        return _ok({"stages": crm_repo.DEFAULT_STAGES, "deals_by_stage": grouped})

    @app.post("/api/crm/deals")
    async def crm_create_deal(body: dict):
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)
        try:
            deal = await asyncio.to_thread(crm_repo.upsert_deal, body or {})
        except ValueError as exc:
            return _err(str(exc), status=400)
        return _ok({"deal": deal})

    @app.put("/api/crm/deals/{deal_id}")
    async def crm_update_deal(deal_id: int, body: dict):
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)
        try:
            deal = await asyncio.to_thread(crm_repo.update_deal, deal_id, body or {})
        except ValueError as exc:
            return _err(str(exc), status=400)
        if not deal:
            return _err("Oportunidade não encontrada.", status=404)
        return _ok({"deal": deal})

    @app.get("/api/crm/deals/{deal_id}/tasks")
    async def crm_list_tasks(deal_id: int):
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)
        deal = await asyncio.to_thread(crm_repo.get_deal, deal_id)
        if not deal:
            return _err("Oportunidade não encontrada.", status=404)
        tasks = await asyncio.to_thread(crm_repo.list_tasks, deal_id)
        return _ok({"tasks": tasks})

    @app.post("/api/crm/deals/{deal_id}/tasks")
    async def crm_create_task(deal_id: int, body: dict):
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)
        deal = await asyncio.to_thread(crm_repo.get_deal, deal_id)
        if not deal:
            return _err("Oportunidade não encontrada.", status=404)
        try:
            task = await asyncio.to_thread(crm_repo.create_task, deal_id, body or {})
        except ValueError as exc:
            return _err(str(exc), status=400)
        return _ok({"task": task})

    @app.put("/api/crm/tasks/{task_id}")
    async def crm_update_task(task_id: int, body: dict):
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)
        try:
            task = await asyncio.to_thread(crm_repo.update_task, task_id, body or {})
        except ValueError as exc:
            return _err(str(exc), status=400)
        if not task:
            return _err("Tarefa não encontrada.", status=404)
        return _ok({"task": task})

    @app.delete("/api/crm/tasks/{task_id}")
    async def crm_delete_task(task_id: int):
        if not _crm_enabled():
            return _err("CRM desativado para esta empresa.", status=403)
        deleted = await asyncio.to_thread(crm_repo.delete_task, task_id)
        if not deleted:
            return _err("Tarefa não encontrada.", status=404)
        return _ok({"message": "Tarefa excluída."})
