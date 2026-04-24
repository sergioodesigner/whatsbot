"""WhatsBot — WhatsApp AI Bot with Web GUI.

Supports two modes:
- **Single-tenant** (default): Original behavior, 1 instance = 1 WhatsApp.
- **Multi-tenant SaaS**: Set WHATSBOT_MODE=saas to activate.
"""

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from config.settings import get_data_dir


def main():
    data_dir = get_data_dir()
    mode = os.environ.get("WHATSBOT_MODE", "single").lower()

    if mode == "saas":
        _main_saas(data_dir)
    else:
        _main_single(data_dir)


def _main_single(data_dir: Path):
    """Original single-tenant startup (backward-compatible)."""

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

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                settings.logs_dir / "whatsbot.log",
                encoding="utf-8",
            ),
        ],
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

    # Setup logging first
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                logs_dir / "whatsbot_saas.log",
                encoding="utf-8",
            ),
        ],
    )
    logger = logging.getLogger("whatsbot")
    logger.info("WhatsBot starting (SaaS multi-tenant mode)...")
    is_docker = os.environ.get("WHATSBOT_DOCKER") == "1" or Path("/.dockerenv").exists()
    if is_docker and str(data_dir) != "/data":
        logger.warning(
            "SaaS running in Docker without WHATSBOT_DATA_DIR=/data. "
            "Sessions and tenant databases may be lost on deploy."
        )

    # Initialize the master database
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
