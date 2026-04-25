"""Tenant Registry — manages the lifecycle of all active tenants.

Each tenant gets its own isolated set of runtime objects:
- Settings (reads/writes to its own SQLite database)
- AppState (in-memory state for QR, messages, etc.)
- GOWAManager (subprocess for WhatsApp bridge)
- GOWAClient (HTTP client to the GOWA subprocess)
- AgentHandler (LLM processing with per-tenant config)
- ConnectionManager (WebSocket connections for this tenant's users)
"""

import dataclasses
import logging
from pathlib import Path

from db import db_manager
from db.repositories import tenant_repo
from server.state import ConnectionManager, AppState

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TenantContext:
    """Holds all runtime objects for a single tenant."""
    tenant_id: int
    slug: str
    name: str
    status: str
    db_name: str  # Key in DatabaseManager (e.g. "tenant_empresa1")
    data_dir: Path
    settings: object = None
    state: object = None       # AppState
    gowa_manager: object = None
    gowa_client: object = None
    agent_handler: object = None
    ws_manager: object = None  # ConnectionManager
    gowa_port: int = 0


class TenantRegistry:
    """Manages lifecycle of all active tenants.

    Provides lookup by slug and handles initialization of each tenant's
    isolated environment (database, settings, GOWA, agent, etc.).
    """

    def __init__(self, base_data_dir: Path, web_port: int = 8080):
        self._tenants: dict[str, TenantContext] = {}  # slug -> context
        self._base_data_dir = base_data_dir
        self._web_port = web_port

    # ── Lookup ────────────────────────────────────────────────────────

    def get_by_slug(self, slug: str) -> TenantContext | None:
        """Return the TenantContext for a slug, or None."""
        return self._tenants.get(slug)

    def all(self) -> list[TenantContext]:
        """Return all loaded tenant contexts."""
        return list(self._tenants.values())

    def active(self) -> list[TenantContext]:
        """Return only active tenant contexts."""
        return [t for t in self._tenants.values() if t.status == "active"]

    # ── Initialization ────────────────────────────────────────────────

    def _tenant_data_dir(self, slug: str) -> Path:
        """Return the data directory for a specific tenant."""
        d = self._base_data_dir / "tenants" / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _init_tenant_db(self, slug: str) -> str:
        """Initialize a tenant's SQLite database. Returns the db_name."""
        data_dir = self._tenant_data_dir(slug)
        storages_dir = data_dir / "storages"
        storages_dir.mkdir(parents=True, exist_ok=True)
        db_path = storages_dir / "whatsbot.db"
        db_name = f"tenant_{slug}"
        db_manager.init(db_name, db_path)
        return db_name

    def _init_tenant_runtime(self, tenant_data: dict, db_name: str) -> TenantContext:
        """Create all runtime objects for a tenant."""
        from config.settings import Settings
        from gowa.manager import GOWAManager
        from gowa.client import GOWAClient
        from agent.handler import AgentHandler
        from server.tenant import current_tenant_db

        slug = tenant_data["slug"]
        data_dir = self._tenant_data_dir(slug)
        gowa_port = tenant_data["gowa_port"]

        # Ensure directories exist
        (data_dir / "statics" / "media").mkdir(parents=True, exist_ok=True)
        (data_dir / "statics" / "senditems").mkdir(parents=True, exist_ok=True)
        (data_dir / "statics" / "avatars").mkdir(parents=True, exist_ok=True)
        (data_dir / "logs").mkdir(parents=True, exist_ok=True)

        # Any component that touches repos (Settings, AgentHandler/TagRegistry,
        # memory bootstrap, etc.) must run with this tenant DB context.
        token = current_tenant_db.set(db_name)
        try:
            settings = Settings(data_dir=data_dir)
            # If tenant has its own API key, use it; otherwise fall back to settings
            api_key = tenant_data.get("openrouter_api_key", "") or settings.get("openrouter_api_key", "")

            # Create GOWA manager and client for this tenant
            # Use the shared webhook route and pass tenant via query param.
            # In SaaS mode, middleware resolves ?tenant=<slug> for localhost hosts.
            webhook_url = f"http://127.0.0.1:{self._web_port}/api/webhook?tenant={slug}"
            gowa_manager = GOWAManager(
                port=gowa_port,
                data_dir=data_dir,
                webhook_url=webhook_url,
            )
            gowa_client = GOWAClient(port=gowa_port)

            # Create agent handler for this tenant
            agent_handler = AgentHandler(
                api_key=api_key,
                system_prompt=settings.get("system_prompt", "Você é um assistente útil."),
                max_context_messages=settings.get("max_context_messages", 10),
                inactivity_timeout_min=settings.get("inactivity_timeout_min", 30),
                model=settings.get("model", "openai/gpt-4o-mini"),
                audio_model=settings.get("audio_model", "google/gemini-2.0-flash-001"),
                image_model=settings.get("image_model", "google/gemini-2.0-flash-001"),
                default_ai_enabled=settings.get("default_ai_enabled", True),
            )

            # Per-tenant WebSocket manager and state
            ws_manager = ConnectionManager()
            state = AppState()

            ctx = TenantContext(
                tenant_id=tenant_data["id"],
                slug=slug,
                name=tenant_data["name"],
                status=tenant_data["status"],
                db_name=db_name,
                data_dir=data_dir,
                settings=settings,
                state=state,
                gowa_manager=gowa_manager,
                gowa_client=gowa_client,
                agent_handler=agent_handler,
                ws_manager=ws_manager,
                gowa_port=gowa_port,
            )
        finally:
            current_tenant_db.reset(token)

        # Set up GOWA restart callback
        def _on_gowa_restart():
            gowa_client.reset()
            state.qr_data = None
            state.qr_fetched_at = 0
            state.bot_phone = ""
            state.bot_name = ""

        gowa_manager._on_restart = _on_gowa_restart

        return ctx

    def load_tenant(self, slug: str) -> TenantContext | None:
        """Load (or reload) a single tenant from the master database."""
        tenant_data = tenant_repo.get_by_slug(slug)
        if not tenant_data:
            logger.warning("Tenant '%s' not found in master database.", slug)
            return None

        db_name = self._init_tenant_db(slug)
        ctx = self._init_tenant_runtime(tenant_data, db_name)
        self._tenants[slug] = ctx
        logger.info("Tenant '%s' loaded (port=%d, status=%s).",
                     slug, ctx.gowa_port, ctx.status)
        return ctx

    def load_all(self) -> None:
        """Load all active tenants from the master database."""
        tenants = tenant_repo.list_all(status="active")
        for t in tenants:
            self.load_tenant(t["slug"])
        logger.info("Loaded %d active tenants.", len(self._tenants))

    def create_tenant(self, slug: str, name: str, **kwargs) -> TenantContext:
        """Create a new tenant in the master DB and initialize its runtime."""
        tenant_data = tenant_repo.create(slug, name, **kwargs)
        ctx = self.load_tenant(slug)
        logger.info("Created new tenant '%s' (id=%d, port=%d).",
                     slug, tenant_data["id"], tenant_data["gowa_port"])
        return ctx

    def suspend_tenant(self, slug: str) -> None:
        """Suspend a tenant: stop GOWA, mark as suspended."""
        ctx = self._tenants.get(slug)
        if ctx:
            try:
                ctx.state.stop_event.set()
                ctx.gowa_manager.stop()
            except Exception as e:
                logger.error("Error stopping GOWA for tenant '%s': %s", slug, e)
            ctx.status = "suspended"
        tenant_repo.set_status(slug, "suspended")
        logger.info("Tenant '%s' suspended.", slug)

    def activate_tenant(self, slug: str) -> TenantContext | None:
        """Re-activate a suspended tenant."""
        tenant_repo.set_status(slug, "active")
        return self.load_tenant(slug)

    def remove_tenant(self, slug: str) -> None:
        """Remove a tenant from the registry (does NOT delete database files)."""
        ctx = self._tenants.pop(slug, None)
        if ctx:
            try:
                ctx.state.stop_event.set()
                ctx.gowa_manager.stop()
            except Exception:
                pass
            try:
                ctx.settings.save()
            except Exception:
                pass
        logger.info("Tenant '%s' removed from registry.", slug)

    def stop_all(self) -> None:
        """Gracefully stop all tenants (for server shutdown)."""
        for slug, ctx in self._tenants.items():
            try:
                ctx.state.stop_event.set()
                ctx.gowa_manager.stop()
            except Exception as e:
                logger.error("Error stopping tenant '%s': %s", slug, e)
            try:
                ctx.settings.save()
            except Exception:
                pass
        logger.info("All tenants stopped.")
