"""Configuration endpoints (config, test-key, models, status)."""

import asyncio
import logging
import time
from typing import Any

import httpx

from db.repositories import master_policy_repo, master_billing_repo
from server.tenant import current_tenant_slug
from server.auth import generate_salt, hash_password
from server.helpers import _ok, _err, _mask_key

logger = logging.getLogger(__name__)

# ── Models cache ──────────────────────────────────────────────
_models_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}
_MODELS_CACHE_TTL = 600  # 10 minutes


def get_models_cache() -> dict[str, Any]:
    """Expose models cache for pricing lookup."""
    return _models_cache


def register_routes(app, deps):
    settings = deps.settings
    agent_handler = deps.agent_handler
    ws_manager = deps.ws_manager
    state = deps.state

    def _api_models_policy() -> tuple[bool, bool, bool]:
        """Return (global_enabled, tenant_enabled, effective_enabled)."""
        slug = current_tenant_slug.get()
        global_enabled = True
        tenant_enabled = True
        try:
            global_enabled = bool(master_policy_repo.get_global("api_models_enabled", True))
            if slug and slug not in ("default", "__superadmin__"):
                tenant_enabled = bool(master_policy_repo.get_tenant(slug, "api_models_enabled", True))
        except RuntimeError:
            # Single-tenant mode (no master DB)
            global_enabled = True
            tenant_enabled = True
        return global_enabled, tenant_enabled, global_enabled and tenant_enabled

    def _crm_policy() -> bool:
        slug = current_tenant_slug.get()
        try:
            if slug and slug not in ("default", "__superadmin__"):
                return bool(master_policy_repo.get_tenant(slug, "crm_enabled", True))
        except RuntimeError:
            return True
        return True

    @app.get("/api/config")
    async def get_config():
        global_enabled, tenant_enabled, effective_enabled = _api_models_policy()
        return _ok({
            "openrouter_api_key": _mask_key(settings.get("openrouter_api_key", "")),
            "model": settings.get("model", "openai/gpt-4o-mini"),
            "audio_model": settings.get("audio_model", "google/gemini-2.0-flash-001"),
            "image_model": settings.get("image_model", "google/gemini-2.0-flash-001"),
            "system_prompt": settings.get("system_prompt", ""),
            "auto_reply": settings.get("auto_reply", True),
            "max_context_messages": settings.get("max_context_messages", 10),
            "message_batch_delay": settings.get("message_batch_delay", 3.0),
            "split_messages": settings.get("split_messages", True),
            "split_message_delay": settings.get("split_message_delay", 2.0),
            "audio_transcription_enabled": settings.get("audio_transcription_enabled", True),
            "image_transcription_enabled": settings.get("image_transcription_enabled", True),
            "transfer_alert_enabled": settings.get("transfer_alert_enabled", True),
            "transfer_alert_duration": settings.get("transfer_alert_duration", 5),
            "max_executions": settings.get("max_executions", 200),
            "default_ai_enabled": settings.get("default_ai_enabled", True),
            "has_password": bool(settings.get("web_password_hash", "")),
            "api_models_globally_enabled": global_enabled,
            "api_models_enabled": tenant_enabled,
            "api_models_effective_enabled": effective_enabled,
            "crm_enabled": _crm_policy(),
        })

    @app.put("/api/config")
    async def save_config(body: dict):
        global_enabled, tenant_enabled, effective_enabled = _api_models_policy()
        slug = current_tenant_slug.get()

        if "api_models_enabled" in body and slug and slug not in ("default", "__superadmin__"):
            desired = bool(body.get("api_models_enabled"))
            if desired and not global_enabled:
                return _err("API e modelos estão desativados globalmente pelo Superadmin.", status=403)
            try:
                master_policy_repo.set_tenant(slug, "api_models_enabled", desired)
                tenant_enabled = desired
                effective_enabled = global_enabled and tenant_enabled
            except RuntimeError:
                pass

        if not effective_enabled and any(k in body for k in ("openrouter_api_key", "model", "audio_model", "image_model")):
            return _err("API e modelos estão bloqueados pelo Superadmin para esta empresa.", status=403)

        allowed_keys = {
            "openrouter_api_key", "model", "audio_model", "image_model",
            "audio_transcription_enabled", "image_transcription_enabled",
            "system_prompt", "auto_reply",
            "max_context_messages", "message_batch_delay",
            "split_messages", "split_message_delay",
            "transfer_alert_enabled", "transfer_alert_duration",
            "group_reply_mode", "bot_phone", "bot_name",
            "default_ai_enabled",
        }
        for key, value in body.items():
            if key in allowed_keys:
                settings[key] = value

        # Handle password set/change/remove
        if "web_password" in body:
            raw_password = body["web_password"]
            if raw_password:
                salt = generate_salt()
                settings["web_password_hash"] = hash_password(raw_password, salt)
                settings["web_password_salt"] = salt
                logger.info("Web panel password set/changed.")
            else:
                settings["web_password_hash"] = ""
                settings["web_password_salt"] = ""
                logger.info("Web panel password removed.")

        settings.save()

        agent_handler.update_config(
            api_key=settings.get("openrouter_api_key", ""),
            system_prompt=settings.get("system_prompt", ""),
            model=settings.get("model", "openai/gpt-4o-mini"),
            audio_model=settings.get("audio_model", "google/gemini-2.0-flash-001"),
            image_model=settings.get("image_model", "google/gemini-2.0-flash-001"),
            max_context_messages=settings.get("max_context_messages", 10),
            split_messages=settings.get("split_messages", True),
            default_ai_enabled=settings.get("default_ai_enabled", True),
        )

        await ws_manager.broadcast("config_saved", {})
        logger.info("Config saved.")
        return _ok({"message": "Configurações salvas!"})

    @app.post("/api/config/test-key")
    async def test_api_key(body: dict):
        _, _, effective_enabled = _api_models_policy()
        if not effective_enabled:
            return _err("API e modelos estão bloqueados pelo Superadmin para esta empresa.", status=403)
        api_key = body.get("api_key", "").strip()
        if not api_key:
            return _err("Insira uma API key primeiro.")
        ok, msg = await asyncio.to_thread(agent_handler.test_api_key, api_key)
        # Auto-save valid key
        if ok:
            settings["openrouter_api_key"] = api_key
            settings.save()
            agent_handler.update_config(
                api_key=api_key,
                system_prompt=settings.get("system_prompt", ""),
                model=settings.get("model", "openai/gpt-4o-mini"),
                audio_model=settings.get("audio_model", "google/gemini-2.0-flash-001"),
                image_model=settings.get("image_model", "google/gemini-2.0-flash-001"),
                max_context_messages=settings.get("max_context_messages", 10),
            )
            logger.info("API key tested and auto-saved.")
        return _ok({"valid": ok, "message": msg})

    @app.get("/api/models")
    async def list_models():
        """Return OpenRouter model list (cached for 10 min)."""
        now = time.time()
        if _models_cache["data"] and now - _models_cache["fetched_at"] < _MODELS_CACHE_TTL:
            return _ok(_models_cache["data"])
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://openrouter.ai/api/v1/models")
                resp.raise_for_status()
                raw = resp.json()
            models = []
            for m in raw.get("data", []):
                arch = m.get("architecture", {})
                models.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "input_modalities": arch.get("input_modalities", ["text"]),
                    "pricing": m.get("pricing", {}),
                })
            models.sort(key=lambda x: x["name"].lower())
            _models_cache["data"] = models
            _models_cache["fetched_at"] = now
            return _ok(models)
        except Exception as e:
            logger.error("Failed to fetch OpenRouter models: %s", e)
            if _models_cache["data"]:
                return _ok(_models_cache["data"])
            return _err(f"Erro ao buscar modelos: {e}", status=502)

    @app.get("/api/status")
    async def get_status():
        return _ok({
            "connected": state.connected,
            "msg_count": state.msg_count,
            "auto_reply_running": state.auto_reply_running,
            "notification": state.notification,
            "bot_phone": state.bot_phone,
            "bot_name": state.bot_name,
        })

    @app.get("/api/billing/invoices")
    async def get_billing_invoices():
        """Read-only invoices for current tenant panel."""
        slug = current_tenant_slug.get()
        if not slug or slug in ("default", "__superadmin__"):
            return _ok({"invoices": [], "financial": None})
        try:
            invoices = master_billing_repo.list_invoices(slug)
            financial = master_billing_repo.get_financial_summary(slug)
            return _ok({"invoices": invoices, "financial": financial})
        except RuntimeError:
            # Single-tenant mode may not have master DB.
            return _ok({"invoices": [], "financial": None})
