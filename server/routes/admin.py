"""Superadmin API routes for managing tenants and global SaaS settings."""

import logging
import time
import hmac
import asyncio
import urllib.request
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from db.repositories import tenant_repo
from db.repositories import master_policy_repo
from db.repositories import master_billing_repo
from server.auth import (
    generate_salt,
    hash_password,
    generate_token,
    generate_superadmin_delegate_token,
)
from server.helpers import _ok, _err
from server.tenant import current_tenant_slug, current_tenant_db
from server.routes import update as update_routes
from config.settings import get_data_dir

logger = logging.getLogger(__name__)


def _require_superadmin(request: Request) -> bool:
    """Check if the request is coming from the admin subdomain."""
    return current_tenant_slug.get() == "__superadmin__"


def register_routes(app, registry):
    def _with_tenant(slug: str):
        ctx = registry.get_by_slug(slug)
        if not ctx:
            return None, None
        token = current_tenant_db.set(ctx.db_name)
        return ctx, token

    """Register superadmin routes on the FastAPI app."""

    # ── Auth guard for all admin routes ───────────────────────────────

    def _has_valid_admin_token(request: Request) -> bool:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return False
        token = auth_header[7:].strip()
        if not token:
            return False
        for admin in tenant_repo.list_superadmins():
            expected = generate_token(admin.get("password_hash", ""), admin.get("salt", ""))
            if expected and hmac.compare_digest(token, expected):
                return True
        return False

    def _check_admin(request: Request, *, require_token: bool = True):
        if not _require_superadmin(request):
            return JSONResponse(
                {"ok": False, "error": "Acesso negado."},
                status_code=403,
            )
        if require_token and not _has_valid_admin_token(request):
            return JSONResponse(
                {"ok": False, "error": "Não autenticado."},
                status_code=401,
            )
        return None

    # ── Setup (first-time superadmin creation) ────────────────────────

    @app.get("/api/admin/setup-status")
    async def setup_status(request: Request):
        """Check if the initial superadmin account has been created."""
        guard = _check_admin(request, require_token=False)
        if guard:
            return guard
        return _ok({"needs_setup": not tenant_repo.superadmin_exists()})

    @app.post("/api/admin/setup")
    async def setup(request: Request, body: dict):
        """Create the initial superadmin account (one-time setup)."""
        guard = _check_admin(request, require_token=False)
        if guard:
            return guard
        if tenant_repo.superadmin_exists():
            return _err("Superadmin já configurado.")
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        if not username or not password:
            return _err("Usuário e senha são obrigatórios.")
        if len(password) < 6:
            return _err("A senha deve ter pelo menos 6 caracteres.")

        salt = generate_salt()
        pwd_hash = hash_password(password, salt)
        tenant_repo.create_superadmin(username, pwd_hash, salt)
        token = generate_token(pwd_hash, salt)
        logger.info("Superadmin account created: %s", username)
        return _ok({"message": "Superadmin criado com sucesso!", "token": token})

    @app.post("/api/admin/login")
    async def admin_login(request: Request, body: dict):
        """Authenticate as superadmin."""
        guard = _check_admin(request, require_token=False)
        if guard:
            return guard
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        if not username or not password:
            return _err("Usuário e senha são obrigatórios.")

        admin = tenant_repo.get_superadmin(username)
        if not admin:
            return _err("Credenciais inválidas.")

        pwd_hash = hash_password(password, admin["salt"])
        if pwd_hash != admin["password_hash"]:
            return _err("Credenciais inválidas.")

        token = generate_token(admin["password_hash"], admin["salt"])
        return _ok({"token": token, "username": username})

    # ── Tenants CRUD ──────────────────────────────────────────────────

    @app.get("/api/admin/tenants")
    async def list_tenants(request: Request):
        """List all tenants with their status and runtime info."""
        guard = _check_admin(request)
        if guard:
            return guard

        tenants = tenant_repo.list_all()
        # Enrich with runtime status
        result = []
        for t in tenants:
            ctx = registry.get_by_slug(t["slug"])
            t["whatsapp_connected"] = ctx.state.connected if ctx else False
            t["msg_count"] = ctx.state.msg_count if ctx else 0
            t["financial"] = master_billing_repo.get_financial_summary(t["slug"])
            result.append(t)
        return _ok(result)

    @app.post("/api/admin/tenants")
    async def create_tenant(request: Request, body: dict):
        """Create a new tenant."""
        guard = _check_admin(request)
        if guard:
            return guard

        slug = body.get("slug", "").strip().lower()
        name = body.get("name", "").strip()

        if not slug or not name:
            return _err("Slug e nome são obrigatórios.")

        # Validate slug format (alphanumeric + hyphens only)
        import re
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", slug) or len(slug) < 3:
            return _err("Slug inválido. Use apenas letras minúsculas, números e hífens (mín. 3 caracteres).")

        from server.middleware import RESERVED_SUBDOMAINS
        if slug in RESERVED_SUBDOMAINS:
            return _err(f"O slug '{slug}' é reservado.")

        if tenant_repo.get_by_slug(slug):
            return _err(f"Já existe uma empresa com o slug '{slug}'.")

        kwargs = {}
        if body.get("plan"):
            kwargs["plan"] = body["plan"]
        if body.get("max_contacts"):
            kwargs["max_contacts"] = int(body["max_contacts"])
        if body.get("openrouter_api_key"):
            kwargs["openrouter_api_key"] = body["openrouter_api_key"]

        ctx = registry.create_tenant(slug, name, **kwargs)
        tenant_data = tenant_repo.get_by_slug(slug)
        logger.info("Tenant created via admin: %s (%s)", slug, name)
        return _ok(tenant_data)

    @app.get("/api/admin/tenants/{slug}")
    async def get_tenant(request: Request, slug: str):
        """Get detailed info about a specific tenant."""
        guard = _check_admin(request)
        if guard:
            return guard

        tenant = tenant_repo.get_by_slug(slug)
        if not tenant:
            return _err("Empresa não encontrada.", status=404)

        ctx = registry.get_by_slug(slug)
        tenant["whatsapp_connected"] = ctx.state.connected if ctx else False
        tenant["msg_count"] = ctx.state.msg_count if ctx else 0
        tenant["auto_reply_running"] = ctx.state.auto_reply_running if ctx else False
        tenant["bot_phone"] = ctx.state.bot_phone if ctx else ""
        return _ok(tenant)

    @app.put("/api/admin/tenants/{slug}")
    async def update_tenant(request: Request, slug: str, body: dict):
        """Update tenant fields."""
        guard = _check_admin(request)
        if guard:
            return guard

        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)

        updated = tenant_repo.update(slug, **body)
        logger.info("Tenant updated via admin: %s", slug)
        return _ok(updated)

    @app.post("/api/admin/tenants/{slug}/suspend")
    async def suspend_tenant(request: Request, slug: str):
        """Suspend a tenant (stops GOWA, blocks access)."""
        guard = _check_admin(request)
        if guard:
            return guard

        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)

        registry.suspend_tenant(slug)
        logger.info("Tenant suspended via admin: %s", slug)
        return _ok({"message": f"Empresa '{slug}' suspensa."})

    @app.post("/api/admin/tenants/{slug}/activate")
    async def activate_tenant(request: Request, slug: str):
        """Re-activate a suspended tenant."""
        guard = _check_admin(request)
        if guard:
            return guard

        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)

        registry.activate_tenant(slug)
        logger.info("Tenant activated via admin: %s", slug)
        return _ok({"message": f"Empresa '{slug}' reativada."})

    @app.delete("/api/admin/tenants/{slug}")
    async def delete_tenant(request: Request, slug: str):
        """Delete a tenant (removes from registry, keeps DB files)."""
        guard = _check_admin(request)
        if guard:
            return guard

        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)

        registry.remove_tenant(slug)
        tenant_repo.delete(slug)
        logger.info("Tenant deleted via admin: %s", slug)
        return _ok({"message": f"Empresa '{slug}' removida."})

    @app.post("/api/admin/tenants/{slug}/delegate-token")
    async def tenant_delegate_token(request: Request, slug: str, body: dict | None = None):
        """Issue a short-lived delegated token for superadmin access in one tenant."""
        guard = _check_admin(request)
        if guard:
            return guard

        tenant = tenant_repo.get_by_slug(slug)
        if not tenant:
            return _err("Empresa não encontrada.", status=404)

        ttl_seconds = 600
        if body and body.get("ttl_seconds") is not None:
            try:
                ttl_seconds = int(body.get("ttl_seconds"))
            except (TypeError, ValueError):
                ttl_seconds = 600
        ttl_seconds = max(60, min(1800, ttl_seconds))

        auth_header = request.headers.get("authorization", "")
        token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        if not token:
            return _err("Não autenticado.", status=401)

        for admin_user in tenant_repo.list_superadmins():
            expected = generate_token(
                admin_user.get("password_hash", ""),
                admin_user.get("salt", ""),
            )
            if expected and hmac.compare_digest(token, expected):
                delegated = generate_superadmin_delegate_token(
                    admin_user.get("password_hash", ""),
                    admin_user.get("salt", ""),
                    slug,
                    ttl_seconds=ttl_seconds,
                )
                return _ok(
                    {
                        "token": delegated,
                        "tenant_slug": slug,
                        "expires_in": ttl_seconds,
                    }
                )

        return _err("Não autenticado.", status=401)

    # ── Dashboard / Metrics ───────────────────────────────────────────

    @app.get("/api/admin/dashboard")
    async def admin_dashboard(request: Request):
        """Global SaaS metrics for the superadmin dashboard."""
        guard = _check_admin(request)
        if guard:
            return guard

        total = tenant_repo.count()
        active = tenant_repo.count_active()

        # Aggregate runtime stats across all tenants
        total_messages = 0
        total_connected = 0
        for ctx in registry.all():
            total_messages += ctx.state.msg_count
            if ctx.state.connected:
                total_connected += 1

        return _ok({
            "total_tenants": total,
            "active_tenants": active,
            "connected_whatsapps": total_connected,
            "total_messages": total_messages,
        })

    @app.get("/api/admin/ai-policy")
    async def get_ai_policy(request: Request):
        """Get global AI policy and per-tenant overrides."""
        guard = _check_admin(request)
        if guard:
            return guard
        global_enabled = bool(master_policy_repo.get_global("api_models_enabled", True))
        items = []
        for t in tenant_repo.list_all():
            tenant_enabled = bool(master_policy_repo.get_tenant(t["slug"], "api_models_enabled", True))
            items.append(
                {
                    "slug": t["slug"],
                    "name": t["name"],
                    "enabled": tenant_enabled,
                    "effective_enabled": global_enabled and tenant_enabled,
                }
            )
        return _ok({"global_enabled": global_enabled, "tenants": items})

    @app.put("/api/admin/ai-policy/global")
    async def set_ai_policy_global(request: Request, body: dict):
        """Enable/disable API+models globally across all tenants."""
        guard = _check_admin(request)
        if guard:
            return guard
        enabled = bool(body.get("enabled", True))
        master_policy_repo.set_global("api_models_enabled", enabled)
        return _ok({"global_enabled": enabled})

    @app.put("/api/admin/ai-policy/tenant/{slug}")
    async def set_ai_policy_tenant(request: Request, slug: str, body: dict):
        """Enable/disable API+models for a specific tenant."""
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)
        enabled = bool(body.get("enabled", True))
        master_policy_repo.set_tenant(slug, "api_models_enabled", enabled)
        global_enabled = bool(master_policy_repo.get_global("api_models_enabled", True))
        return _ok({"slug": slug, "enabled": enabled, "effective_enabled": global_enabled and enabled})

    @app.get("/api/admin/update/check")
    async def admin_update_check(request: Request):
        guard = _check_admin(request)
        if guard:
            return guard
        project_root = get_data_dir()
        current = await asyncio.to_thread(update_routes._read_local_version, project_root)
        latest = await asyncio.to_thread(update_routes._fetch_remote_version)
        return _ok({
            "current_version": current,
            "latest_version": latest,
            "update_available": bool(latest and latest != current),
        })

    @app.post("/api/admin/update")
    async def admin_update_apply(request: Request):
        guard = _check_admin(request)
        if guard:
            return guard
        project_root = get_data_dir()
        try:
            msg = await asyncio.to_thread(update_routes._perform_update, project_root)
        except RuntimeError as exc:
            return _err(str(exc), 500)
        except Exception as exc:
            logger.exception("Unexpected admin update error")
            return _err(f"Erro inesperado: {exc}", 500)
        return _ok({"message": msg})

    @app.get("/api/admin/tenants/{slug}/api-settings")
    async def get_tenant_api_settings(request: Request, slug: str):
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)
        global_enabled = bool(master_policy_repo.get_global("api_models_enabled", True))
        tenant_enabled = bool(master_policy_repo.get_tenant(slug, "api_models_enabled", True))
        ctx, token = _with_tenant(slug)
        if not ctx:
            return _err("Tenant não carregado.", status=404)
        try:
            settings = ctx.settings
            return _ok(
                {
                    "api_models_globally_enabled": global_enabled,
                    "api_models_enabled": tenant_enabled,
                    "api_models_effective_enabled": global_enabled and tenant_enabled,
                    "openrouter_api_key": settings.get("openrouter_api_key", ""),
                    "model": settings.get("model", "openai/gpt-4o-mini"),
                    "audio_model": settings.get("audio_model", "google/gemini-2.0-flash-001"),
                    "image_model": settings.get("image_model", "google/gemini-2.0-flash-001"),
                    "max_executions": settings.get("max_executions", 200),
                    "crm_enabled": bool(master_policy_repo.get_tenant(slug, "crm_enabled", True)),
                }
            )
        finally:
            current_tenant_db.reset(token)

    @app.put("/api/admin/tenants/{slug}/api-settings")
    async def save_tenant_api_settings(request: Request, slug: str, body: dict):
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)

        global_enabled = bool(master_policy_repo.get_global("api_models_enabled", True))
        if "api_models_enabled" in body:
            desired = bool(body.get("api_models_enabled"))
            if desired and not global_enabled:
                return _err("API e modelos estão desativados globalmente.", status=403)
            master_policy_repo.set_tenant(slug, "api_models_enabled", desired)
        if "crm_enabled" in body:
            master_policy_repo.set_tenant(slug, "crm_enabled", bool(body.get("crm_enabled")))

        ctx, token = _with_tenant(slug)
        if not ctx:
            return _err("Tenant não carregado.", status=404)
        try:
            settings = ctx.settings
            allowed = {"openrouter_api_key", "model", "audio_model", "image_model", "max_executions"}
            for key in allowed:
                if key in body:
                    settings[key] = body[key]
            settings.save()
            ctx.agent_handler.update_config(
                api_key=settings.get("openrouter_api_key", ""),
                system_prompt=settings.get("system_prompt", ""),
                model=settings.get("model", "openai/gpt-4o-mini"),
                audio_model=settings.get("audio_model", "google/gemini-2.0-flash-001"),
                image_model=settings.get("image_model", "google/gemini-2.0-flash-001"),
                max_context_messages=settings.get("max_context_messages", 10),
                split_messages=settings.get("split_messages", True),
                default_ai_enabled=settings.get("default_ai_enabled", True),
            )
            return _ok({"message": "Configurações de API salvas."})
        finally:
            current_tenant_db.reset(token)

    @app.get("/api/admin/tenants/{slug}/company")
    async def get_tenant_company(request: Request, slug: str):
        guard = _check_admin(request)
        if guard:
            return guard
        tenant = tenant_repo.get_by_slug(slug)
        if not tenant:
            return _err("Empresa não encontrada.", status=404)
        profile = master_billing_repo.get_profile(slug)
        invoices = master_billing_repo.list_invoices(slug)
        financial = master_billing_repo.get_financial_summary(slug)
        return _ok({"tenant": tenant, "profile": profile, "invoices": invoices, "financial": financial})

    @app.put("/api/admin/tenants/{slug}/company")
    async def save_tenant_company(request: Request, slug: str, body: dict):
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)
        profile = master_billing_repo.upsert_profile(slug, body)
        return _ok({"profile": profile})

    @app.put("/api/admin/tenants/{slug}/invoices/{period_ym}")
    async def upsert_tenant_invoice(request: Request, slug: str, period_ym: str, body: dict):
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)
        payload = dict(body or {})
        payload["period_ym"] = period_ym
        try:
            invoice = master_billing_repo.upsert_invoice(slug, payload)
        except ValueError as exc:
            return _err(str(exc), status=400)
        summary = master_billing_repo.get_financial_summary(slug)
        return _ok({"invoice": invoice, "financial": summary})

    @app.delete("/api/admin/tenants/{slug}/invoices/{period_ym}")
    async def delete_tenant_invoice(request: Request, slug: str, period_ym: str):
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)
        deleted = master_billing_repo.delete_invoice(slug, period_ym)
        if not deleted:
            return _err("Fatura não encontrada.", status=404)
        return _ok({"message": "Fatura excluída.", "financial": master_billing_repo.get_financial_summary(slug)})

    @app.get("/api/admin/tenants/{slug}/invoices/ensure")
    async def ensure_tenant_invoices(request: Request, slug: str):
        guard = _check_admin(request)
        if guard:
            return guard
        if not tenant_repo.get_by_slug(slug):
            return _err("Empresa não encontrada.", status=404)
        master_billing_repo.ensure_next_three_open_invoices(slug)
        return _ok({"invoices": master_billing_repo.list_invoices(slug)})

    @app.get("/api/admin/models")
    async def admin_models(request: Request):
        guard = _check_admin(request)
        if guard:
            return guard
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/models",
                headers={"Accept": "application/json", "User-Agent": "WhatsBot-Superadmin"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            items = []
            for m in raw.get("data", []):
                items.append({"id": m.get("id", ""), "name": m.get("name", "")})
            items.sort(key=lambda x: (x["name"] or "").lower())
            return _ok(items)
        except Exception as exc:
            return _err(f"Erro ao buscar modelos: {exc}", status=502)
