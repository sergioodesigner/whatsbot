"""WhatsBot — FastAPI backend with REST API, WebSocket and background tasks.

Supports two modes:
- **Single-tenant** (default): Backward-compatible, no multi-tenant overhead.
- **Multi-tenant SaaS**: Activated by WHATSBOT_MODE=saas environment variable.
"""

import asyncio
import dataclasses
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from server.auth import auth_required, verify_token, verify_superadmin_delegate_token
from server.helpers import _get_web_dir
from server.state import MemoryLogHandler, ConnectionManager, AppState
from server.background import start_gowa_task, status_poll_loop, qr_poll_loop, avatar_fetch_task
from server.routes import logs, sandbox, config, whatsapp, websocket, usage, contacts, webhook, auth, tags, executions, update
from server.auth import generate_token
from db.repositories import tenant_repo

logger = logging.getLogger(__name__)

# ── In-memory log capture (attach to root logger) ────────────────────────

_memory_log_handler = MemoryLogHandler()
_memory_log_handler.setLevel(logging.DEBUG)
_root = logging.getLogger()
_root.addHandler(_memory_log_handler)
# Ensure root logger level allows INFO+ through to handlers
if _root.level == logging.NOTSET or _root.level > logging.INFO:
    _root.setLevel(logging.INFO)


# ── Server Dependencies ──────────────────────────────────────────────────

@dataclasses.dataclass
class ServerDeps:
    """Container for shared dependencies passed to route modules."""
    settings: object
    gowa_manager: object
    gowa_client: object
    agent_handler: object
    ws_manager: ConnectionManager
    state: AppState
    memory_log_handler: MemoryLogHandler
    statics_senditems_dir: Path
    # Dynamically set by webhook route for cross-module access
    broadcast_tool_calls: object = None


# ── Multi-tenant Deps Proxy ──────────────────────────────────────────────

class TenantAwareDeps:
    """Proxy that resolves deps from the current tenant's context.

    Routes continue using ``deps.settings``, ``deps.gowa_client``, etc.
    without any code changes — the proxy transparently returns the correct
    instance based on the current_tenant_slug contextvar.
    """

    class _TenantObjectProxy:
        """Lazily resolves a tenant-scoped object on each access.

        This allows route modules to keep the existing pattern of binding
        `settings = deps.settings` at registration time, while still using the
        correct tenant object at request time.
        """

        def __init__(self, resolver):
            self._resolver = resolver

        def _obj(self):
            return self._resolver()

        def __getattr__(self, name):
            return getattr(self._obj(), name)

        def __getitem__(self, key):
            return self._obj()[key]

        def __setitem__(self, key, value):
            self._obj()[key] = value

        def __contains__(self, item):
            return item in self._obj()

        def __iter__(self):
            return iter(self._obj())

        def __len__(self):
            return len(self._obj())

        def __bool__(self):
            return bool(self._obj())

        def __str__(self):
            return str(self._obj())

        def __repr__(self):
            return repr(self._obj())

        def __fspath__(self):
            return os.fspath(self._obj())

        def __truediv__(self, other):
            return self._obj() / other

    def __init__(self, registry, memory_log_handler):
        self._registry = registry
        self.memory_log_handler = memory_log_handler
        # broadcast_tool_calls is set per-tenant after webhook registers
        self.broadcast_tool_calls = None
        self._settings_proxy = self._TenantObjectProxy(lambda: self._current().settings)
        self._gowa_manager_proxy = self._TenantObjectProxy(lambda: self._current().gowa_manager)
        self._gowa_client_proxy = self._TenantObjectProxy(lambda: self._current().gowa_client)
        self._agent_handler_proxy = self._TenantObjectProxy(lambda: self._current().agent_handler)
        self._ws_manager_proxy = self._TenantObjectProxy(lambda: self._current().ws_manager)
        self._state_proxy = self._TenantObjectProxy(lambda: self._current().state)
        self._statics_senditems_dir_proxy = self._TenantObjectProxy(
            lambda: self._current().data_dir / "statics" / "senditems"
        )

    def _current(self):
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()
        ctx = self._registry.get_by_slug(slug)
        if ctx is None:
            raise RuntimeError(f"No tenant context for slug '{slug}'")
        return ctx

    @property
    def settings(self):
        return self._settings_proxy

    @property
    def gowa_manager(self):
        return self._gowa_manager_proxy

    @property
    def gowa_client(self):
        return self._gowa_client_proxy

    @property
    def agent_handler(self):
        return self._agent_handler_proxy

    @property
    def ws_manager(self):
        return self._ws_manager_proxy

    @property
    def state(self):
        return self._state_proxy

    @property
    def statics_senditems_dir(self):
        return self._statics_senditems_dir_proxy


# ── Factory (Single-Tenant — existing behavior) ──────────────────────────

def create_app(
    settings,
    gowa_manager,
    gowa_client,
    agent_handler,
) -> FastAPI:
    """Create and configure the FastAPI application (single-tenant mode)."""

    ws_manager = ConnectionManager()
    state = AppState()
    web_dir = _get_web_dir()

    # Prepare statics directories
    statics_dir = settings.data_dir / "statics"
    statics_media_dir = statics_dir / "media"
    statics_senditems_dir = statics_dir / "senditems"
    statics_media_dir.mkdir(parents=True, exist_ok=True)
    statics_senditems_dir.mkdir(parents=True, exist_ok=True)

    deps = ServerDeps(
        settings=settings,
        gowa_manager=gowa_manager,
        gowa_client=gowa_client,
        agent_handler=agent_handler,
        ws_manager=ws_manager,
        state=state,
        memory_log_handler=_memory_log_handler,
        statics_senditems_dir=statics_senditems_dir,
    )

    # ── GOWA restart callback ──────────────────────────────────────────
    def _on_gowa_restart():
        gowa_client.reset()
        state.qr_data = None
        state.qr_fetched_at = 0
        state.bot_phone = ""
        state.bot_name = ""

    gowa_manager._on_restart = _on_gowa_restart

    # ── Lifespan ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.stop_event.clear()
        tasks = [
            asyncio.create_task(start_gowa_task(deps)),
            asyncio.create_task(status_poll_loop(deps)),
            asyncio.create_task(qr_poll_loop(deps)),
            asyncio.create_task(avatar_fetch_task(deps)),
        ]
        yield
        # Shutdown
        state.stop_event.set()
        for task in tasks:
            task.cancel()
        try:
            settings.save()
        except Exception:
            pass
        try:
            gowa_manager.stop()
        except Exception:
            pass
        logger.info("Server shutdown complete.")

    # ── FastAPI App ───────────────────────────────────────────────────

    app = FastAPI(title="WhatsBot", lifespan=lifespan)

    # Mount static files (frontend assets)
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Mount statics/ for GOWA media files (auto-downloaded images, audio, etc.)
    app.mount("/statics", StaticFiles(directory=str(statics_dir)), name="statics")

    # ── Auth middleware ────────────────────────────────────────────────

    # Paths exempt from authentication
    _AUTH_EXEMPT_PREFIXES = ("/static/", "/statics/", "/api/webhook", "/api/auth/", "/health")
    _SPA_PATHS = {"/", "/dashboard", "/sandbox", "/costs", "/executions"}

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path

        # SPA pages, static assets, webhook, and auth endpoints are always open
        if path in _SPA_PATHS or path.startswith(("/contacts/",)):
            return await call_next(request)
        for prefix in _AUTH_EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Only protect /api/* paths
        if path.startswith("/api/") and auth_required(settings):
            auth_header = request.headers.get("authorization", "")
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]
            if not token or not verify_token(token, settings):
                return JSONResponse(
                    {"ok": False, "error": "Não autenticado."},
                    status_code=401,
                )

        return await call_next(request)

    # ── Health endpoint (always open, used by Docker healthcheck) ──────

    @app.get("/health")
    async def healthcheck():
        return JSONResponse({"ok": True})

    # ── Frontend routes ────────────────────────────────────────────────

    @app.get("/")
    @app.get("/dashboard")
    @app.get("/sandbox")
    @app.get("/costs")
    @app.get("/executions")
    @app.get("/contacts/{contact_id:int}")
    async def index(contact_id: int | None = None):
        index_file = web_dir / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return JSONResponse({"error": "Frontend not found"}, status_code=404)

    # ── Register route modules ─────────────────────────────────────────
    # Order matters: webhook must be registered before sandbox so
    # broadcast_tool_calls is available via deps.
    auth.register_routes(app, deps)
    webhook.register_routes(app, deps)
    logs.register_routes(app, deps)
    sandbox.register_routes(app, deps)
    config.register_routes(app, deps)
    whatsapp.register_routes(app, deps)
    websocket.register_routes(app, deps)
    usage.register_routes(app, deps)
    contacts.register_routes(app, deps)
    tags.register_routes(app, deps)
    executions.register_routes(app, deps)
    update.register_routes(app, deps)

    return app


# ── Factory (Multi-Tenant SaaS) ──────────────────────────────────────────

def create_saas_app(registry, base_domain: str) -> FastAPI:
    """Create and configure the FastAPI application in multi-tenant SaaS mode.

    Args:
        registry: Initialized TenantRegistry with all active tenants loaded.
        base_domain: Base domain for subdomain routing (e.g. "whatsbot.com").
    """
    from server.middleware import create_tenant_middleware
    from server.routes import admin
    from server.tenant import current_tenant_db

    web_dir = _get_web_dir()
    deps = TenantAwareDeps(registry, _memory_log_handler)

    # ── Lifespan ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start background tasks for each tenant
        all_tasks = []
        for ctx in registry.active():
            ctx.state.stop_event.clear()
            # Build a ServerDeps for background tasks (they need concrete objects)
            tenant_deps = ServerDeps(
                settings=ctx.settings,
                gowa_manager=ctx.gowa_manager,
                gowa_client=ctx.gowa_client,
                agent_handler=ctx.agent_handler,
                ws_manager=ctx.ws_manager,
                state=ctx.state,
                memory_log_handler=_memory_log_handler,
                statics_senditems_dir=ctx.data_dir / "statics" / "senditems",
            )

            # Wrap each background task to set the correct tenant context
            async def _with_tenant_ctx(coro_fn, t_deps, db_name):
                token = current_tenant_db.set(db_name)
                try:
                    await coro_fn(t_deps)
                finally:
                    current_tenant_db.reset(token)

            all_tasks.extend([
                asyncio.create_task(_with_tenant_ctx(start_gowa_task, tenant_deps, ctx.db_name)),
                asyncio.create_task(_with_tenant_ctx(status_poll_loop, tenant_deps, ctx.db_name)),
                asyncio.create_task(_with_tenant_ctx(qr_poll_loop, tenant_deps, ctx.db_name)),
                asyncio.create_task(_with_tenant_ctx(avatar_fetch_task, tenant_deps, ctx.db_name)),
            ])

        logger.info("Started %d background tasks for %d tenants.",
                     len(all_tasks), len(registry.active()))
        yield

        # Shutdown
        registry.stop_all()
        for task in all_tasks:
            task.cancel()
        logger.info("SaaS server shutdown complete.")

    # ── FastAPI App ───────────────────────────────────────────────────

    app = FastAPI(title="WhatsBot SaaS", lifespan=lifespan)

    # Mount static files (frontend assets — shared across all tenants)
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── Tenant middleware (MUST be before auth middleware) ─────────────

    tenant_mw = create_tenant_middleware(registry, base_domain)
    app.middleware("http")(tenant_mw)

    # ── Auth middleware ────────────────────────────────────────────────

    _AUTH_EXEMPT_PREFIXES = (
        "/static/", "/statics/", "/api/webhook", "/api/auth/",
        "/api/admin/setup", "/api/admin/login", "/health",
    )
    _SPA_PATHS = {"/", "/dashboard", "/sandbox", "/costs", "/executions"}
    _SUPERADMIN_ONLY_SPA_PATHS = {"/sandbox", "/costs", "/executions"}
    _SUPERADMIN_ONLY_API_PREFIXES = ("/api/sandbox", "/api/usage", "/api/executions")
    _DELEGATED_ALLOWED_SPA_PATHS = {"/sandbox", "/costs", "/executions"}
    _DELEGATED_ALLOWED_API_PREFIXES = ("/api/sandbox", "/api/usage", "/api/executions", "/api/status", "/api/tenant/info")

    def _extract_superadmin_token(request: Request) -> str:
        token = (request.headers.get("x-superadmin-token", "") or "").strip()
        if token:
            return token
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:].strip()
            if bearer:
                return bearer
        return (request.query_params.get("sa_token", "") or "").strip()

    def _delegated_tenant_slug(request: Request) -> str:
        from server.tenant import current_tenant_slug
        slug = (current_tenant_slug.get() or "").strip()
        if slug and slug not in ("default", "__superadmin__"):
            return slug
        header_slug = (request.headers.get("x-superadmin-tenant", "") or "").strip().lower()
        if header_slug:
            return header_slug
        query_slug = (request.query_params.get("sa_tenant", "") or "").strip().lower()
        return query_slug

    def _has_valid_superadmin_token(request: Request) -> bool:
        token = _extract_superadmin_token(request)
        if not token:
            return False
        slug = _delegated_tenant_slug(request)
        if not slug:
            return False
        for admin_user in tenant_repo.list_superadmins():
            pwd_hash = admin_user.get("password_hash", "")
            salt = admin_user.get("salt", "")

            # New delegated token format (short-lived and tenant-bound)
            if verify_superadmin_delegate_token(
                token,
                password_hash=pwd_hash,
                salt=salt,
                tenant_slug=slug,
            ):
                return True

            # Backward-compatible fallback (legacy superadmin session token)
            expected = generate_token(pwd_hash, salt)
            if expected and hmac.compare_digest(token, expected):
                return True
        return False

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        from server.tenant import current_tenant_slug
        token_candidate = _extract_superadmin_token(request)

        # On admin subdomain, only admin APIs are valid.
        # This prevents tenant route handlers from raising RuntimeError
        # when old/default frontend assets call /api/auth/*, /api/config, etc.
        if (
            path.startswith("/api/")
            and current_tenant_slug.get() == "__superadmin__"
            and not path.startswith("/api/admin/")
        ):
            return JSONResponse(
                {"ok": False, "error": "Endpoint disponível apenas para tenants."},
                status_code=404,
            )

        superadmin_delegated = False

        # Sandbox/Costs/Executions are restricted to superadmin delegated access.
        if path in _SUPERADMIN_ONLY_SPA_PATHS or any(
            path.startswith(prefix) for prefix in _SUPERADMIN_ONLY_API_PREFIXES
        ):
            if not _has_valid_superadmin_token(request):
                return JSONResponse(
                    {"ok": False, "error": "Acesso restrito ao Superadmin."},
                    status_code=403,
                )
            superadmin_delegated = True

        # If a delegated token is being used, lock navigation to delegated-safe paths.
        if token_candidate and _has_valid_superadmin_token(request):
            delegated_slug = _delegated_tenant_slug(request)
            from server.tenant import current_tenant_slug
            current_slug = (current_tenant_slug.get() or "").strip()
            if current_slug == "__superadmin__" or not delegated_slug:
                return JSONResponse(
                    {"ok": False, "error": "Token delegado inválido para este domínio."},
                    status_code=403,
                )
            delegated_spa = path in _DELEGATED_ALLOWED_SPA_PATHS
            delegated_api = any(path.startswith(prefix) for prefix in _DELEGATED_ALLOWED_API_PREFIXES)
            if not delegated_spa and not delegated_api:
                return JSONResponse(
                    {"ok": False, "error": "Acesso delegado permite apenas Custos, Execuções e Sandbox."},
                    status_code=403,
                )
            superadmin_delegated = True

        if path in _SPA_PATHS or path.startswith(("/contacts/",)):
            return await call_next(request)
        for prefix in _AUTH_EXEMPT_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # Superadmin routes have their own auth check
        if path.startswith("/api/admin/"):
            return await call_next(request)

        # Tenant auth
        if path.startswith("/api/") and not superadmin_delegated:
            try:
                settings = deps.settings
                if auth_required(settings):
                    auth_header = request.headers.get("authorization", "")
                    token = ""
                    if auth_header.startswith("Bearer "):
                        token = auth_header[7:]
                    if not token or not verify_token(token, settings):
                        return JSONResponse(
                            {"ok": False, "error": "Não autenticado."},
                            status_code=401,
                        )
            except RuntimeError:
                # No tenant context (e.g. base domain without subdomain)
                pass

        return await call_next(request)

    # ── Health endpoint ───────────────────────────────────────────────

    @app.get("/health")
    async def healthcheck():
        return JSONResponse({"ok": True})

    # ── Tenant info endpoint ──────────────────────────────────────────

    @app.get("/api/tenant/info")
    async def tenant_info():
        """Return current tenant's public info (for frontend branding)."""
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()
        ctx = registry.get_by_slug(slug)
        if not ctx:
            return JSONResponse({"ok": False, "error": "Tenant não encontrado"}, status_code=404)
        return {"ok": True, "data": {"name": ctx.name, "slug": ctx.slug}}

    @app.get("/statics/{file_path:path}")
    async def tenant_statics(file_path: str):
        """Serve tenant-specific static files (avatars, media, senditems) in SaaS mode."""
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()
        ctx = registry.get_by_slug(slug)
        if not ctx:
            return JSONResponse({"ok": False, "error": "Tenant não encontrado"}, status_code=404)

        statics_root = (ctx.data_dir / "statics").resolve()
        target = (statics_root / file_path).resolve()
        if not str(target).startswith(str(statics_root)):
            return JSONResponse({"ok": False, "error": "Caminho inválido"}, status_code=400)
        if not target.is_file():
            return JSONResponse({"ok": False, "error": "Arquivo não encontrado"}, status_code=404)
        return FileResponse(str(target))

    # ── Frontend routes ────────────────────────────────────────────────

    @app.get("/")
    @app.get("/dashboard")
    @app.get("/sandbox")
    @app.get("/costs")
    @app.get("/executions")
    @app.get("/contacts/{contact_id:int}")
    async def index(request: Request, contact_id: int | None = None):
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()

        # Superadmin gets the admin panel
        if slug == "__superadmin__":
            admin_file = web_dir / "admin.html"
            if admin_file.exists():
                return FileResponse(str(admin_file))
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Frontend admin não encontrado. Verifique web/admin.html no deploy.",
                },
                status_code=500,
            )

        # Regular tenant gets the standard panel
        index_file = web_dir / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return JSONResponse({"error": "Frontend not found"}, status_code=404)

    # ── Webhook route (tenant-aware, path-based) ──────────────────────

    @app.post("/api/webhook/{tenant_slug}")
    async def saas_webhook(tenant_slug: str, body: dict):
        # Keep this route only as a guardrail. The canonical SaaS webhook endpoint
        # is /api/webhook?tenant=<slug>, which is fully processed by webhook routes.
        return JSONResponse(
            {
                "ok": False,
                "error": "Use /api/webhook?tenant=<slug> para processamento completo do webhook.",
            },
            status_code=410,
        )

    # ── Register route modules (using tenant-aware deps proxy) ────────

    auth.register_routes(app, deps)
    webhook.register_routes(app, deps)
    logs.register_routes(app, deps)
    sandbox.register_routes(app, deps)
    config.register_routes(app, deps)
    whatsapp.register_routes(app, deps)
    websocket.register_routes(app, deps)
    usage.register_routes(app, deps)
    contacts.register_routes(app, deps)
    tags.register_routes(app, deps)
    executions.register_routes(app, deps)
    update.register_routes(app, deps)

    # Register superadmin routes
    admin.register_routes(app, registry)

    return app
