"""Tenant resolution middleware for multi-tenant SaaS mode.

Extracts the tenant slug from the request Host header (subdomain),
resolves it via the TenantRegistry, and sets the appropriate contextvars
so that downstream code (get_db(), routes, etc.) operates on the correct
tenant's data.
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from server.tenant import current_tenant_db, current_tenant_slug

logger = logging.getLogger(__name__)

# Subdomains that are reserved and cannot be used as tenant slugs
RESERVED_SUBDOMAINS = {"admin", "api", "www", "app", "static", "assets", "mail"}


def create_tenant_middleware(registry, base_domain: str):
    """Create a middleware function that resolves the current tenant.

    Args:
        registry: TenantRegistry instance.
        base_domain: The base domain (e.g. "whatsbot.com").
    """

    async def tenant_middleware(request: Request, call_next):
        # Prefer original host forwarded by reverse proxies (Railway/Nginx).
        forwarded_host = request.headers.get("x-forwarded-host", "")
        if forwarded_host:
            # X-Forwarded-Host may include a list: client, proxy1, proxy2...
            host = forwarded_host.split(",")[0].strip().split(":")[0].lower()
        else:
            host = request.headers.get("host", "").split(":")[0].lower()  # strip port
        # Reset context at the start of each request to avoid accidental leaks.
        current_tenant_slug.set("default")
        current_tenant_db.set("default")

        # ── Superadmin panel ──────────────────────────────────────
        if host == f"admin.{base_domain}" or host == "admin" or host.startswith("admin."):
            current_tenant_slug.set("__superadmin__")
            return await call_next(request)

        # ── Tenant webhook (GOWA calls don't have Host subdomain) ─
        # Webhooks are routed via path: /api/webhook/{tenant_slug}
        path = request.url.path
        if path.startswith("/api/webhook/"):
            slug = path.split("/")[3] if len(path.split("/")) > 3 else ""
            if slug:
                tenant = registry.get_by_slug(slug)
                if tenant:
                    current_tenant_slug.set(slug)
                    current_tenant_db.set(tenant.db_name)
                    return await call_next(request)
                else:
                    return JSONResponse(
                        {"ok": False, "error": f"Tenant '{slug}' não encontrado."},
                        status_code=404,
                    )

        # Webhook query-mode: /api/webhook?tenant=<slug>
        # This is used by internal GOWA callbacks in SaaS mode.
        if path == "/api/webhook":
            slug = (request.query_params.get("tenant", "") or "").strip().lower()
            if slug:
                tenant = registry.get_by_slug(slug)
                if tenant:
                    current_tenant_slug.set(slug)
                    current_tenant_db.set(tenant.db_name)
                    return await call_next(request)
                return JSONResponse(
                    {"ok": False, "error": f"Tenant '{slug}' não encontrado."},
                    status_code=404,
                )

        # ── Resolve tenant from subdomain ─────────────────────────
        subdomain = ""
        if host.endswith(f".{base_domain}"):
            subdomain = host[: -(len(base_domain) + 1)]
        elif "." not in host:
            # Local development: treat host as slug directly (e.g. localhost)
            # In dev mode, use query param ?tenant=slug or fall back to default
            subdomain = request.query_params.get("tenant", "")

        # Fallback for environments where WHATSBOT_DOMAIN may not exactly match
        # the incoming host suffix. If a known tenant exists in the first label
        # of the host, resolve it directly.
        if not subdomain and "." in host:
            first_label = host.split(".", 1)[0]
            if first_label and first_label not in RESERVED_SUBDOMAINS:
                candidate = registry.get_by_slug(first_label)
                if candidate:
                    current_tenant_slug.set(first_label)
                    current_tenant_db.set(candidate.db_name)
                    return await call_next(request)

        # No subdomain resolved — might be the base domain itself
        if not subdomain or subdomain in RESERVED_SUBDOMAINS:
            # In SaaS mode, tenant API routes must always have tenant context.
            # Otherwise route handlers may raise RuntimeError when reading deps.
            if path.startswith("/api/") and not path.startswith(("/api/admin/", "/api/webhook")):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Tenant não resolvido. Acesse via subdomínio da empresa.",
                    },
                    status_code=404,
                )
            # Allow non-API routes to pass through (landing page/admin frontend)
            return await call_next(request)

        tenant = registry.get_by_slug(subdomain)
        if not tenant:
            return JSONResponse(
                {"ok": False, "error": "Empresa não encontrada."},
                status_code=404,
            )
        if tenant.status != "active":
            return JSONResponse(
                {"ok": False, "error": "Conta suspensa. Entre em contato com o administrador."},
                status_code=403,
            )

        # Set context for this request
        current_tenant_slug.set(subdomain)
        current_tenant_db.set(tenant.db_name)

        return await call_next(request)

    return tenant_middleware
