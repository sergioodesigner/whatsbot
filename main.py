"""WhatsBot — WhatsApp AI Bot with Web GUI.

Supports two modes:
- **Single-tenant** (default): Original behavior, 1 instance = 1 WhatsApp.
- **Multi-tenant SaaS**: Set WHATSBOT_MODE=saas to activate.
"""

import logging
import logging.handlers
import os
import sys
import threading
import webbrowser
from pathlib import Path

from config.settings import get_data_dir

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _truthy_env(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _handlers_with_optional_file(log_path: Path | None, *, default_file: bool) -> list[logging.Handler]:
    """Stdout always; file only if enabled and the filesystem accepts writes."""
    fmt = logging.Formatter(_LOG_FORMAT)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    handlers: list[logging.Handler] = [out]
    if log_path is None:
        return handlers
    if not _truthy_env("WHATSBOT_FILE_LOG", default_file):
        return handlers
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=int(os.environ.get("WHATSBOT_LOG_MAX_BYTES", "5242880")),
            backupCount=int(os.environ.get("WHATSBOT_LOG_BACKUP_COUNT", "3")),
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        handlers.append(fh)
    except OSError as exc:
        print(
            f"[whatsbot] Log em arquivo desativado ({exc!s}); usando apenas stdout.",
            file=sys.stderr,
        )
    return handlers


def main():
    data_dir = get_data_dir()
    mode = os.environ.get("WHATSBOT_MODE", "single").lower()

    if mode == "saas":
        _main_saas(data_dir)
    else:
        _main_single(data_dir)


def _main_single(data_dir: Path):
    """Original single-tenant startup (backward-compatible)."""

    # ── Phase 1: Storage provider (local or Supabase) ─────────────────
    from db.storage_provider import init_provider as _init_storage
    _init_storage(data_dir)

    # Initialize SQLite database before anything else
    from db import init_db
    is_docker = os.environ.get("WHATSBOT_DOCKER") == "1" or Path("/.dockerenv").exists()
    storages_dir = data_dir / "storages"
    storages_dir.mkdir(exist_ok=True)
    db_path = storages_dir / "whatsbot.db"
    init_db(db_path)

    # Auto-migrate from JSON files if DB is empty
    from db.migrate_json import needs_migration, migrate
    if needs_migration(data_dir):
        migrate(data_dir)

    # Now load settings (reads from SQLite)
    from config.settings import Settings
    settings = Settings()

    # Setup logging (arquivo opcional; evita ENOSPC em volumes pequenos)
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=_handlers_with_optional_file(
            settings.logs_dir / "whatsbot.log",
            default_file=True,
        ),
        force=True,
    )
    logger = logging.getLogger("whatsbot")
    logger.info("WhatsBot starting (single-tenant mode)...")

    from gowa.manager import GOWAManager
    from gowa.client import GOWAClient
    from agent.handler import AgentHandler
    from server.app import create_app

    port = settings.get("gowa_port", 3000)
    web_port = settings.get("web_port", 8080)

    webhook_url = f"http://127.0.0.1:{web_port}/api/webhook"
    gowa_manager = GOWAManager(port=port, data_dir=settings.data_dir, webhook_url=webhook_url)
    gowa_client = GOWAClient(port=port)

    agent_handler = AgentHandler(
        api_key=settings.get("openrouter_api_key", ""),
        system_prompt=settings.get("system_prompt", "Você é um assistente útil."),
        max_context_messages=settings.get("max_context_messages", 10),
        inactivity_timeout_min=settings.get("inactivity_timeout_min", 30),
        model=settings.get("model", "openai/gpt-4o-mini"),
        audio_model=settings.get("audio_model", "google/gemini-2.0-flash-001"),
        image_model=settings.get("image_model", "google/gemini-2.0-flash-001"),
        default_ai_enabled=settings.get("default_ai_enabled", True),
    )

    app = create_app(
        settings=settings,
        gowa_manager=gowa_manager,
        gowa_client=gowa_client,
        agent_handler=agent_handler,
    )

    host = "0.0.0.0"

    # Open browser after server has time to start (skip in Docker — no display)
    if not is_docker:
        url = f"http://127.0.0.1:{web_port}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    logger.info("Starting web server on http://%s:%d", host, web_port)
    uvicorn.run(app, host=host, port=web_port, log_level="warning")
    logger.info("WhatsBot exiting.")


def _main_saas(data_dir: Path):
    """Multi-tenant SaaS startup."""

    # Logging: em PaaS o volume costuma ser pequeno — por padrão só stdout
    # (Railway captura stdout). Arquivo: WHATSBOT_FILE_LOG=1
    _on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    _default_file = not _on_railway
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=_handlers_with_optional_file(
            data_dir / "logs" / "whatsbot_saas.log",
            default_file=_default_file,
        ),
        force=True,
    )
    logger = logging.getLogger("whatsbot")
    logger.info("WhatsBot starting (SaaS multi-tenant mode)...")
    is_docker = os.environ.get("WHATSBOT_DOCKER") == "1" or Path("/.dockerenv").exists()
    if is_docker and str(data_dir) != "/data":
        logger.warning(
            "SaaS running in Docker without WHATSBOT_DATA_DIR=/data. "
            "Sessions and tenant databases may be lost on deploy."
        )

    # ── Phase 1: Storage provider (local or Supabase) ─────────────────
    from db.storage_provider import init_provider as _init_storage
    _init_storage(data_dir)

    # ── Phase 2: Master database (SQLite or Supabase Postgres) ────────
    _master_backend = os.environ.get("MASTER_DB_BACKEND", "sqlite").strip().lower()
    if _master_backend == "supabase":
        from db.master_pg_connection import init_master_pg
        _pg_url = os.environ.get("SUPABASE_DB_URL", "").strip()
        if not _pg_url:
            logger.error(
                "MASTER_DB_BACKEND=supabase but SUPABASE_DB_URL is not set. "
                "Falling back to SQLite master DB."
            )
            # Override env var so the repo proxy resolves the SQLite module
            os.environ["MASTER_DB_BACKEND"] = "sqlite"
            _master_backend = "sqlite"
        else:
            init_master_pg(_pg_url)
            logger.info("Master database ready (Supabase Postgres).")
            
            # Phase 3: CRM/Automations schema (runs only if CRM_AUTOMATION_BACKEND=supabase)
            from db.tenant_pg_connection import init_tenant_pg_schema
            init_tenant_pg_schema()

    if _master_backend != "supabase":
        from db.master_connection import init_master_db
        master_db_path = data_dir / "master.db"
        init_master_db(master_db_path)
        logger.info("Master database ready at %s", master_db_path)


    # Create superadmin if none exists (prompt for initial setup via API)
    from db.repositories import tenant_repo
    if not tenant_repo.superadmin_exists():
        logger.warning(
            "No superadmin account found. "
            "Access admin.YOUR_DOMAIN to complete initial setup."
        )

    # Read configuration from environment
    web_port = int(os.environ.get("WHATSBOT_WEB_PORT", "8080"))
    base_domain = os.environ.get("WHATSBOT_DOMAIN", "localhost")

    # Initialize the tenant registry and load all active tenants
    from server.tenant_registry import TenantRegistry
    registry = TenantRegistry(base_data_dir=data_dir, web_port=web_port)
    registry.load_all()
    logger.info("Loaded %d tenants.", len(registry.all()))

    # Create the SaaS FastAPI application
    from server.app import create_saas_app
    app = create_saas_app(registry, base_domain)

    host = "0.0.0.0"
    import uvicorn
    logger.info("Starting SaaS web server on http://%s:%d (domain: %s)",
                host, web_port, base_domain)
    uvicorn.run(app, host=host, port=web_port, log_level="warning")
    logger.info("WhatsBot SaaS exiting.")


if __name__ == "__main__":
    main()
