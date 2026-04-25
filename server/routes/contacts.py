"""Contact CRUD and messaging endpoints."""

import asyncio
import logging
import re
import shutil
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from gowa.client import GOWASendError, extract_msg_id

from db.repositories import contact_repo, message_repo, crm_repo
from server.helpers import _ok, _err
from server.media_urls import enrich_message_media_path

logger = logging.getLogger(__name__)


def register_routes(app, deps):
    agent_handler = deps.agent_handler
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings
    statics_senditems_dir = deps.statics_senditems_dir

    def _resolve_media_dirs() -> tuple[Path, Path]:
        """Resolve tenant-aware media directories at request time.

        In SaaS mode, deps.statics_senditems_dir is a tenant proxy and cannot be
        dereferenced during route registration (no tenant context yet).
        """
        senditems_dir = Path(statics_senditems_dir)
        senditems_dir.mkdir(parents=True, exist_ok=True)
        media_dir = senditems_dir.parent / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        return senditems_dir, media_dir

    def _extract_meta(html: str, prop: str) -> str:
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
            rf'<meta[^>]+name=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(prop)}["\']',
        ]
        for p in patterns:
            m = re.search(p, html, re.IGNORECASE)
            if m:
                return unescape(m.group(1).strip())
        return ""

    @app.get("/api/link-preview")
    async def get_link_preview(url: str = ""):
        """Fetch a lightweight OpenGraph preview for a URL."""
        url = (url or "").strip()
        if not url:
            return _err("URL obrigatória.")
        if not url.startswith(("http://", "https://")):
            return _err("URL inválida.")

        parsed = urlparse(url)
        if not parsed.netloc:
            return _err("URL inválida.")

        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"
                        )
                    },
                )
            ctype = (resp.headers.get("content-type", "") or "").lower()
            if "text/html" not in ctype:
                return _ok({"url": url, "site_name": parsed.netloc, "title": parsed.netloc})

            html = resp.text[:300000]
            title = _extract_meta(html, "og:title")
            description = _extract_meta(html, "og:description") or _extract_meta(html, "description")
            image = _extract_meta(html, "og:image")
            site_name = _extract_meta(html, "og:site_name") or parsed.netloc
            if not title:
                t = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                title = unescape(t.group(1).strip()) if t else parsed.netloc

            return _ok({
                "url": str(resp.url),
                "title": title[:200],
                "description": description[:300] if description else "",
                "image": image,
                "site_name": site_name[:120],
            })
        except Exception as e:
            logger.warning("[LinkPreview] Failed for %s: %s", url, e)
            return _err("Não foi possível carregar prévia do link.", status=502)

    def _normalize_media_path(path: str | None) -> str:
        """Normalize legacy media paths into a stable relative path."""
        if not path:
            return ""
        clean = str(path).strip().strip('"').strip("'").replace("\\", "/")
        if ";" in clean:
            clean = clean.split(";", 1)[0].strip()
        if not clean:
            return ""
        if clean.startswith(("http://", "https://", "blob:")):
            return clean
        if "/statics/" in clean:
            clean = clean.split("/statics/", 1)[1]
            return f"statics/{clean.lstrip('/')}"
        if clean.startswith("/statics/"):
            return clean.lstrip("/")
        if clean.startswith("statics/"):
            return clean
        if clean.startswith("media/") or clean.startswith("senditems/"):
            return f"statics/{clean}"
        if "/" not in clean:
            return f"statics/media/{clean}"
        return clean.lstrip("/")

    def _is_remote_media_path(path: str | None) -> bool:
        if not path:
            return False
        clean = str(path).strip()
        return clean.startswith(("http://", "https://", "blob:"))

    def _should_repair_media_path(path: str | None) -> bool:
        """Avoid expensive repair on already-good/remote paths."""
        if not path:
            return False
        clean = str(path).strip().strip('"').strip("'")
        if not clean or _is_remote_media_path(clean):
            return False
        if clean.startswith("statics/"):
            return False
        if clean.startswith("/statics/"):
            return False
        return True

    def _repair_media_path(media_path: str | None) -> str | None:
        """Backfill old incoming media files into statics/media when possible."""
        if _is_remote_media_path(media_path):
            return media_path
        raw_clean = str(media_path or "").strip().strip('"').strip("'").replace("\\", "/")
        normalized = _normalize_media_path(media_path)
        if not normalized:
            return media_path

        data_dir = Path(settings.data_dir)
        filename = Path(normalized).name
        raw_filename = Path(raw_clean).name if raw_clean else ""
        candidate_names = [n for n in dict.fromkeys([filename, raw_filename]) if n]
        
        from db.storage_provider import get_provider
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()
        if not slug or slug == "default":
            slug = "single_tenant_default"
        object_key = f"{slug}/{filename}"

        def _upload_file(src_path: Path) -> str:
            import mimetypes
            mime, _ = mimetypes.guess_type(str(src_path))
            content = src_path.read_bytes()
            url = get_provider().upload("media", object_key, content, content_type=mime or "application/octet-stream")
            return url

        candidates: list[Path] = []
        for name in candidate_names:
            candidates.extend([
                data_dir / normalized,
                data_dir / normalized.replace("statics/", "", 1),
                data_dir / "media" / name,
                data_dir / "storages" / "media" / name,
                data_dir / "storages" / "statics" / "media" / name,
                data_dir / "statics" / "media" / name,
                data_dir / "statics" / "senditems" / name,
            ])

        for src in candidates:
            try:
                if src.is_file():
                    url = _upload_file(src)
                    return url
            except Exception:
                continue

        # Deep fallback for historical records: scan tenant folders recursively.
        for root in (data_dir, data_dir / "storages"):
            try:
                if not root.exists():
                    continue
                for name in candidate_names:
                    for found in root.rglob(name):
                        if found.is_file():
                            url = _upload_file(found)
                            logger.info("[Contacts] Legacy media repaired from recursive exact match: %s", found)
                            return url
                prefix = Path(filename).stem
                for found in root.rglob(f"{prefix}*"):
                    if found.is_file():
                        url = _upload_file(found)
                        logger.info("[Contacts] Legacy media repaired from recursive prefix match: %s", found)
                        return url
            except Exception:
                continue

        logger.warning(
            "[Contacts] Legacy media not found for repair. raw=%r normalized=%r tried=%s",
            media_path, normalized, [str(p) for p in candidates]
        )
        return normalized

    async def _send_read_receipts(phone: str, msg_ids: list[str]):
        """Send read receipts to GOWA in background (best-effort)."""
        for mid in msg_ids:
            try:
                await asyncio.to_thread(gowa_client.mark_as_read, mid, phone)
                logger.info("[ReadReceipt] Sent for %s msg %s", phone, mid)
            except Exception as e:
                logger.warning("[ReadReceipt] Failed for %s msg %s: %s", phone, mid, e)

    @app.get("/api/contacts")
    async def list_contacts(q: str = "", archived: bool = False):
        """List all contacts with summary info."""
        results = await asyncio.to_thread(contact_repo.list_contacts, q, archived)
        return _ok(results)

    @app.post("/api/contacts/check-phone")
    async def check_phone(request: Request):
        """Check if a phone number is registered on WhatsApp."""
        body = await request.json()
        phone = (body.get("phone") or "").strip()
        if not phone:
            return _err("Campo 'phone' é obrigatório.")

        # Normalize: strip non-digits, ensure country code
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) < 10:
            return _err("Número inválido. Informe DDD + número.")
        if not digits.startswith("55"):
            digits = "55" + digits

        try:
            result = await asyncio.to_thread(gowa_client.check_phone, digits)
        except GOWASendError as e:
            return _err(f"Erro ao verificar número: {e}")

        registered = result.get("registered", False)
        name = result.get("name", "")
        # Use canonical phone from WhatsApp (avoids BR 12/13 digit duplicates)
        canonical = result.get("canonical_phone", digits) if registered else digits

        # If registered, pre-create contact with WhatsApp name and AI setting
        if registered:
            ai_default = settings.get("default_ai_enabled", True)
            def _save():
                contact_repo.get_or_create(canonical, default_ai_enabled=ai_default)
                if name:
                    c = contact_repo.get_by_phone(canonical)
                    if c and not c["name"]:
                        contact_repo.update(c["id"], name=f"~{name}")
            await asyncio.to_thread(_save)

        return _ok({
            "phone": canonical,
            "registered": registered,
            "jid": result.get("jid", ""),
            "name": name,
        })

    @app.get("/api/contacts/{phone}")
    async def get_contact(
        phone: str,
        mark_read: bool = True,
        messages_full: bool = False,
        messages_limit: int = 400,
    ):
        """Return full contact data including conversation history."""
        def _load():
            data = contact_repo.get_full_contact(phone)
            if data is None:
                # Auto-create contact for verified phone numbers
                ai_default = settings.get("default_ai_enabled", True)
                contact_repo.get_or_create(phone, default_ai_enabled=ai_default)
                data = contact_repo.get_full_contact(phone)
            if data is None:
                return None, []
            contact_id = data["id"]
            # Mark as read when viewing contact (skip if mark_read=false)
            msg_ids = []
            if mark_read and (data.get("unread_count", 0) > 0 or data.get("unread_ai_count", 0) > 0):
                msg_ids = contact_repo.mark_as_read(contact_id)
                data["unread_count"] = 0
                data["unread_ai_count"] = 0
                # Update in-memory cache
                if phone in agent_handler._contacts:
                    agent_handler._contacts[phone].unread_count = 0
                    agent_handler._contacts[phone].unread_ai_count = 0
            # Load messages and repair legacy media paths on-the-fly.
            lim = 5000 if messages_full else max(50, min(int(messages_limit or 400), 5000))
            messages = (
                message_repo.get_all(contact_id)
                if messages_full
                else message_repo.get_recent(contact_id, lim)
            )
            for m in messages:
                media_path = m.get("media_path")
                if m.get("media_type") in ("audio", "image", "video", "gif") and _should_repair_media_path(media_path):
                    repaired = _repair_media_path(m.get("media_path"))
                    if repaired and repaired != m.get("media_path"):
                        m["media_path"] = repaired
                enrich_message_media_path(m)
            data["messages"] = messages
            if not messages_full:
                data["messages_window"] = lim
                data["messages_may_have_older"] = len(messages) >= lim
            # Load usage for the full response
            data["usage"] = []
            # CRM snapshot for contact quick visibility in chat panel
            data["crm"] = crm_repo.get_deal_by_phone(phone)
            return data, msg_ids
        data, msg_ids = await asyncio.to_thread(_load)
        if data is None:
            return _err("Contato não encontrado.", status=404)
        if msg_ids:
            asyncio.create_task(_send_read_receipts(phone, msg_ids))
        # Check group send permissions (fresh check on every contact load)
        if data.get("is_group") and state.bot_phone:
            try:
                can_send = await asyncio.to_thread(
                    gowa_client.can_bot_send_in_group, phone, state.bot_phone)
                if data.get("can_send", True) != can_send:
                    await asyncio.to_thread(
                        contact_repo.update, data["id"], can_send=1 if can_send else 0)
                    data["can_send"] = can_send
                    if phone in agent_handler._contacts:
                        agent_handler._contacts[phone].can_send = can_send
            except Exception as e:
                logger.warning("[Contact] Failed to check group send permission: %s", e)
        return _ok(data)

    @app.delete("/api/contacts/{phone}")
    async def delete_contact(phone: str):
        """Permanently delete a contact and all associated data."""
        def _delete():
            data = contact_repo.get_by_phone(phone)
            if data is None:
                return False
            contact_repo.delete(data["id"])
            # Clear in-memory cache
            agent_handler._contacts.pop(phone, None)
            return True
        found = await asyncio.to_thread(_delete)
        if not found:
            return _err("Contato não encontrado.", status=404)
        logger.info("[Contact] Deleted contact %s", phone)
        await ws_manager.broadcast("contact_deleted", {"phone": phone})
        return _ok({"message": "Contato apagado."})

    @app.post("/api/contacts/{phone}/archive")
    async def archive_contact(phone: str, body: dict):
        """Archive or unarchive a contact (by app)."""
        archived = body.get("archived")
        if archived is None:
            return _err("Campo 'archived' é obrigatório.")
        def _archive():
            data = contact_repo.get_by_phone(phone)
            if data is None:
                return None
            contact_repo.set_archived(data["id"], bool(archived), by_app=True)
            # Update in-memory cache
            if phone in agent_handler._contacts:
                agent_handler._contacts[phone].is_archived = bool(archived)
                agent_handler._contacts[phone].archived_by_app = bool(archived)
            return bool(archived)
        result = await asyncio.to_thread(_archive)
        if result is None:
            return _err("Contato não encontrado.", status=404)
        logger.info("[Contact] %s contact %s", "Archived" if result else "Unarchived", phone)
        await ws_manager.broadcast("contact_archived", {"phone": phone, "archived": result})
        return _ok({"archived": result})

    @app.post("/api/contacts/{phone}/send")
    async def send_to_contact(phone: str, body: dict):
        """Send a manual message to a contact (operator-initiated, no LLM)."""
        message = (body.get("message") or "").strip()
        if not message:
            return _err("Campo 'message' é obrigatório.")

        # Track sent message to filter GOWA echo-backs (must be before send)
        state.recently_sent[f"{phone}:{message[:120]}"] = time.time()

        # Try to send via GOWA — always save message (with status on failure)
        send_failed = False
        error_msg = ""
        send_result = None
        try:
            send_result = await asyncio.to_thread(gowa_client.send_message, phone, message)
        except GOWASendError as e:
            logger.error("[Send] Failed to send message to %s: %s", phone, e)
            send_failed = True
            error_msg = str(e)
        except Exception as e:
            logger.error("[Send] Failed to send message to %s: %s", phone, e)
            send_failed = True
            error_msg = str(e)

        msg_id = extract_msg_id(send_result) if not send_failed else None

        # Always save to contact memory (with status="failed" if send failed)
        try:
            msg_data = await asyncio.to_thread(
                agent_handler.save_operator_message, phone, message,
                status="failed" if send_failed else "sent",
                msg_id=msg_id,
            )
        except Exception as e:
            logger.error("[Send] Failed to save message for %s: %s", phone, e)
            return _err(f"Erro ao salvar mensagem: {e}", status=500)

        if send_failed:
            # Broadcast error event for frontend toast/error bubble
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar mensagem: {error_msg}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar mensagem: {error_msg}", status=500)

        logger.info("[Send] Manual message to %s: %s", phone, message[:80])

        # Broadcast to all WS clients
        enrich_message_media_path(msg_data)
        await ws_manager.broadcast("new_message", {
            "phone": phone,
            "message": msg_data,
        })

        return _ok({"message": "Mensagem enviada.", "msg_id": msg_id})

    @app.post("/api/contacts/{phone}/retry-send")
    async def retry_send_to_contact(phone: str, body: dict):
        """Retry sending a message that previously failed."""
        message = (body.get("message") or "").strip()
        if not message:
            return _err("Campo 'message' é obrigatório.")

        # Track for echo-back filtering
        state.recently_sent[f"{phone}:{message[:120]}"] = time.time()

        send_result = None
        try:
            send_result = await asyncio.to_thread(gowa_client.send_message, phone, message)
        except GOWASendError as e:
            logger.error("[Retry] Failed to resend to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao reenviar mensagem: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao reenviar: {e}", status=500)
        except Exception as e:
            logger.error("[Retry] Failed to resend to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao reenviar: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao reenviar: {e}", status=500)

        msg_id = extract_msg_id(send_result)

        # Mark the existing failed message as sent
        try:
            await asyncio.to_thread(agent_handler.mark_message_sent, phone, message, msg_id)
        except Exception as e:
            logger.error("[Retry] Failed to update message status for %s: %s", phone, e)
            return _err(f"Mensagem reenviada, mas falhou ao atualizar status local: {e}", status=500)

        state.msg_count += 1
        logger.info("[Retry] Resent to %s: %s", phone, message[:80])
        return _ok({"message": "Mensagem reenviada.", "msg_id": msg_id})

    @app.post("/api/contacts/{phone}/send-image")
    async def send_image_to_contact(
        phone: str,
        image: UploadFile = File(...),
        caption: str = Form(""),
    ):
        """Send an image to a contact (operator-initiated)."""
        suffix = Path(image.filename or "img.png").suffix or ".png"
        filename = f"{int(time.time() * 1000)}{suffix}"
        content = await image.read()

        from db.storage_provider import get_provider
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()
        if not slug or slug == "default":
            slug = "single_tenant_default"
        
        object_key = f"{slug}/{filename}"
        mime = image.content_type or "image/png"
        rel_path = ""
        try:
            rel_path = get_provider().upload("media", object_key, content, content_type=mime)
        except Exception as e:
            logger.warning("[Send] Could not upload image to StorageProvider: %s", e)

        send_result = None
        try:
            send_result = await asyncio.to_thread(gowa_client.send_image, phone, caption=caption, image_data=content, filename=filename)
        except GOWASendError as e:
            logger.error("[Send] Failed to send image to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar imagem: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar imagem: {e}", status=500)
        except Exception as e:
            logger.error("[Send] Failed to send image to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao enviar imagem: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao enviar imagem: {e}", status=500)

        msg_id = extract_msg_id(send_result)

        if not rel_path:
            rel_path = f"statics/media/{filename}"

        msg_data = {
            "role": "assistant",
            "content": caption,
            "ts": time.time(),
            "media_type": "image",
            "media_path": rel_path,
            "status": "sent",
            "msg_id": msg_id,
        }
        contact = agent_handler._get_contact(phone)
        contact.add_message("assistant", caption, media_type="image", media_path=rel_path,
                            status="sent", msg_id=msg_id)

        enrich_message_media_path(msg_data)
        await ws_manager.broadcast("new_message", {"phone": phone, "message": msg_data})
        logger.info("[Send] Image sent to %s", phone)
        return _ok({"message": "Imagem enviada."})

    @app.post("/api/contacts/{phone}/send-audio")
    async def send_audio_to_contact(
        phone: str,
        audio: UploadFile = File(...),
    ):
        """Send an audio file to a contact (operator-initiated)."""
        suffix = Path(audio.filename or "voice.ogg").suffix or ".ogg"
        filename = f"{int(time.time() * 1000)}{suffix}"
        content = await audio.read()

        from db.storage_provider import get_provider
        from server.tenant import current_tenant_slug
        slug = current_tenant_slug.get()
        if not slug or slug == "default":
            slug = "single_tenant_default"
        
        object_key = f"{slug}/{filename}"
        mime = audio.content_type or "audio/ogg"
        rel_path = ""
        try:
            rel_path = get_provider().upload("media", object_key, content, content_type=mime)
        except Exception as e:
            logger.warning("[Send] Could not upload audio to StorageProvider: %s", e)

        send_result = None
        try:
            send_result = await asyncio.to_thread(gowa_client.send_audio, phone, audio_data=content, filename=filename)
        except GOWASendError as e:
            logger.error("[Send] Failed to send audio to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Falha ao enviar áudio: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Falha ao enviar áudio: {e}", status=500)
        except Exception as e:
            logger.error("[Send] Failed to send audio to %s: %s", phone, e)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "error",
                    "content": f"Erro inesperado ao enviar áudio: {e}",
                    "ts": time.time(),
                },
            })
            return _err(f"Erro ao enviar áudio: {e}", status=500)

        msg_id = extract_msg_id(send_result)

        if not rel_path:
            rel_path = f"statics/media/{filename}"
            
        msg_data = {
            "role": "assistant",
            "content": "[Áudio]",
            "ts": time.time(),
            "media_type": "audio",
            "media_path": rel_path,
            "status": "sent",
            "msg_id": msg_id,
        }
        contact = agent_handler._get_contact(phone)
        contact.add_message("assistant", "[Áudio]", media_type="audio", media_path=rel_path,
                            status="sent", msg_id=msg_id)

        enrich_message_media_path(msg_data)
        await ws_manager.broadcast("new_message", {"phone": phone, "message": msg_data})
        logger.info("[Send] Audio sent to %s", phone)
        return _ok({"message": "Áudio enviado."})

    @app.post("/api/contacts/{phone}/presence")
    async def send_presence_to_contact(phone: str, body: dict):
        """Send typing/stop presence indicator to a contact (operator-initiated)."""
        action = body.get("action", "start")
        await asyncio.to_thread(gowa_client.send_chat_presence, phone, action)
        return _ok({"status": "ok"})

    @app.post("/api/contacts/{phone}/read")
    async def mark_contact_read(phone: str):
        """Mark all messages from this contact as read (reset unread_count)."""
        def _mark():
            contact = agent_handler._get_contact(phone)
            return contact.mark_as_read()
        msg_ids = await asyncio.to_thread(_mark)
        if msg_ids:
            asyncio.create_task(_send_read_receipts(phone, msg_ids))
        return _ok({"message": "Marcado como lido."})

    @app.post("/api/contacts/{phone}/toggle-ai")
    async def toggle_contact_ai(phone: str, body: dict):
        """Enable or disable AI auto-reply for a specific contact."""
        enabled = body.get("enabled")
        if enabled is None:
            return _err("Campo 'enabled' é obrigatório.")
        def _toggle():
            contact = agent_handler._get_contact(phone)
            contact.set_ai_enabled(bool(enabled))
            return contact.ai_enabled
        result = await asyncio.to_thread(_toggle)
        await ws_manager.broadcast("contact_ai_toggled", {
            "phone": phone,
            "ai_enabled": result,
        })
        return _ok({"ai_enabled": result})

    @app.get("/api/contacts/{phone}/avatar")
    async def get_contact_avatar(phone: str):
        """Return contact's WhatsApp profile photo (cached on disk or in storage)."""
        from db.storage_provider import get_provider
        from server.tenant import current_tenant_slug
        from fastapi.responses import RedirectResponse
        import httpx

        slug = current_tenant_slug.get() or "single_tenant_default"
        object_key = f"{slug}/{phone}.jpg"
        provider = get_provider()

        url = provider.public_url("avatars", object_key)
        
        # Fast local path for single-tenant or local mode
        avatars_dir = statics_senditems_dir.parent / "avatars"
        avatars_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = avatars_dir / f"{phone}.jpg"

        exists = False
        if url.startswith("http"):
            try:
                with httpx.Client(timeout=3.0) as client:
                    resp = client.head(url)
                    exists = resp.status_code < 400
            except Exception:
                pass
        else:
            exists = avatar_path.exists()

        if exists:
            if url.startswith("http"):
                return RedirectResponse(url)
            return FileResponse(str(avatar_path), media_type="image/jpeg")

        # Fetch from GOWA on-demand
        try:
            data = await asyncio.to_thread(gowa_client.get_avatar, phone)
        except Exception:
            data = None

        if data and isinstance(data, bytes) and len(data) > 100:
            new_url = provider.upload("avatars", object_key, data, content_type="image/jpeg")
            if new_url.startswith("http"):
                return RedirectResponse(new_url)
            else:
                return FileResponse(str(avatar_path), media_type="image/jpeg")

        return Response(status_code=204)

    @app.put("/api/contacts/{phone}/info")
    async def update_contact_info(phone: str, body: dict):
        """Update contact info fields (name, email, profession, company, observations)."""
        def _update():
            contact = agent_handler._get_contact(phone)
            # Update scalar fields via update_info
            contact.update_info(
                name=body.get("name", ""),
                email=body.get("email", ""),
                profession=body.get("profession", ""),
                company=body.get("company", ""),
            )
            # Observations: replace entire list (update_info only appends)
            if "observations" in body:
                new_obs = [
                    o for o in body["observations"] if isinstance(o, str) and o.strip()
                ]
                contact.info["observations"] = new_obs
                contact_repo.set_observations(contact.id, new_obs)
            return contact.info
        info = await asyncio.to_thread(_update)
        return _ok(info)
