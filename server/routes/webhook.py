"""Webhook endpoint — receives real-time messages from GOWA."""

import asyncio
import json
import logging
import mimetypes
import random
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
from gowa.client import GOWASendError, extract_msg_id

from db.repositories import contact_repo, message_repo
from server.execution import astart_execution, aend_execution, atrack_step, prune_executions
from server.helpers import _ok

logger = logging.getLogger(__name__)


def _normalize_media_path(path: str | None) -> str | None:
    """Normalize media paths coming from GOWA payloads.

    GOWA can append codec metadata like "; codecs=opus" to file names.
    Keep only the actual path segment so static file serving works.
    """
    if not path:
        return None
    clean = str(path).strip().strip('"').strip("'")
    if ";" in clean:
        clean = clean.split(";", 1)[0].strip()
    if not clean:
        return None
    clean = clean.replace("\\", "/").strip()

    # Strip absolute prefixes and keep project-relative static paths.
    if "/statics/" in clean:
        clean = clean.split("/statics/", 1)[1]
        clean = f"statics/{clean.lstrip('/')}"
    elif clean.startswith("statics/"):
        pass
    elif clean.startswith("media/"):
        clean = f"statics/{clean}"
    elif clean.startswith("senditems/"):
        clean = f"statics/{clean}"
    elif "/" not in clean:
        # GOWA sometimes sends only the filename for media payloads.
        clean = f"statics/media/{clean}"

    return clean


def register_routes(app, deps):
    agent_handler = deps.agent_handler
    gowa_client = deps.gowa_client
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings

    def _sync_media_to_statics(media_path: str | None, wait_for_file: bool = False) -> str | None:
        """Ensure incoming media files are accessible under /statics.

        GOWA may save downloads under data_dir/media while the web panel serves
        files from data_dir/statics. This function mirrors files into
        data_dir/statics/media and returns a stable relative path.
        """
        if not media_path:
            return None

        raw_clean = str(media_path).strip().strip('"').strip("'").replace("\\", "/")
        normalized = _normalize_media_path(media_path)
        if not normalized:
            return None

        data_dir = Path(settings.data_dir)
        statics_media_dir = data_dir / "statics" / "media"
        statics_media_dir.mkdir(parents=True, exist_ok=True)

        # Keep only filename inside statics/media to avoid unexpected nesting.
        filename = Path(normalized).name
        raw_filename = Path(raw_clean).name if raw_clean else ""
        candidate_names = [n for n in dict.fromkeys([filename, raw_filename]) if n]
        dest = statics_media_dir / filename
        if dest.exists():
            return f"statics/media/{filename}"

        candidates: list[Path] = []
        for name in candidate_names:
            candidates.extend([
                data_dir / normalized,
                data_dir / normalized.replace("statics/", "", 1),
                data_dir / "media" / name,
                data_dir / "storages" / "media" / name,
                data_dir / "storages" / "statics" / "media" / name,
                data_dir / "statics" / "media" / name,
            ])
        for src in candidates:
            try:
                if src.is_file():
                    shutil.copy2(src, dest)
                    return f"statics/media/{filename}"
            except Exception:
                continue

        # Fallback: search recursively inside tenant data dir (and storages).
        search_roots = [data_dir, data_dir / "storages"]
        for root in search_roots:
            try:
                if not root.exists():
                    continue
                for name in candidate_names:
                    for found in root.rglob(name):
                        if found.is_file():
                            shutil.copy2(found, dest)
                            logger.info("[Webhook] Media synced from recursive exact match: %s", found)
                            return f"statics/media/{filename}"

                # Some GOWA builds may append codec suffixes to the saved filename.
                # Search by stable prefix and copy to a clean target filename.
                prefix = Path(filename).stem
                for found in root.rglob(f"{prefix}*"):
                    if found.is_file():
                        shutil.copy2(found, dest)
                        logger.info("[Webhook] Media synced from recursive prefix match: %s", found)
                        return f"statics/media/{filename}"
            except Exception:
                continue

        # Optional retry window for eventual consistency:
        # GOWA may write media shortly after webhook delivery.
        if wait_for_file:
            for _ in range(6):  # up to ~3s
                time.sleep(0.5)
                for src in candidates:
                    try:
                        if src.is_file():
                            shutil.copy2(src, dest)
                            return f"statics/media/{filename}"
                    except Exception:
                        continue

        logger.warning(
            "[Webhook] Media file not found for sync. raw=%r normalized=%r tried=%s",
            media_path, normalized, [str(p) for p in candidates]
        )
        return normalized

    def _download_media_from_url(media_url: str, fallback_ext: str = "bin") -> str | None:
        """Download media from URL and save into tenant statics/media."""
        if not media_url or not str(media_url).startswith(("http://", "https://")):
            return None
        data_dir = Path(settings.data_dir)
        statics_media_dir = data_dir / "statics" / "media"
        statics_media_dir.mkdir(parents=True, exist_ok=True)

        url_no_query = str(media_url).split("?", 1)[0]
        ext = Path(url_no_query).suffix.lower().lstrip(".")
        if not ext:
            ext = fallback_ext
        ext = re.sub(r"[^a-z0-9]", "", ext.lower()) or fallback_ext
        filename = f"{int(time.time())}-{uuid.uuid4()}.{ext}"
        dest = statics_media_dir / filename

        try:
            with httpx.Client(timeout=25.0, follow_redirects=True) as client:
                resp = client.get(str(media_url))
                resp.raise_for_status()
                if not resp.content:
                    return None
                dest.write_bytes(resp.content)
                return f"statics/media/{filename}"
        except Exception as e:
            logger.warning("[Webhook] Failed to download media URL %s: %s", media_url, e)
            return None

    def _download_media_from_message_id(
        message_id: str,
        fallback_ext: str = "bin",
        original_filename: str = "",
        original_media_type: str = "",
    ) -> str | None:
        """Download media using official GOWA message download endpoint."""
        if not message_id:
            return None
        data, content_type = gowa_client.download_message_media(message_id)
        if not data:
            return None

        ext = ""
        if original_filename:
            ext = Path(original_filename).suffix.lower().lstrip(".")
        if not ext and content_type:
            mime = content_type.split(";", 1)[0].strip().lower()
            guessed = mimetypes.guess_extension(mime) or ""
            ext = guessed.lstrip(".")
        if not ext and original_media_type:
            mime = str(original_media_type).split(";", 1)[0].strip().lower()
            guessed = mimetypes.guess_extension(mime) or ""
            ext = guessed.lstrip(".")
        ext = re.sub(r"[^a-z0-9]", "", (ext or fallback_ext).lower()) or fallback_ext

        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", message_id)[:80]
        filename = f"{int(time.time())}-{safe_id}.{ext}"
        statics_media_dir = Path(settings.data_dir) / "statics" / "media"
        statics_media_dir.mkdir(parents=True, exist_ok=True)
        dest = statics_media_dir / filename
        try:
            dest.write_bytes(data)
            logger.info("[Webhook] Media downloaded via /message/{id}/download -> %s", filename)
            return f"statics/media/{filename}"
        except Exception as e:
            logger.warning("[Webhook] Failed to persist downloaded media for %s: %s", message_id, e)
            return None

    def _media_exists(rel_path: str | None) -> bool:
        if not rel_path:
            return False
        clean = _normalize_media_path(rel_path)
        if not clean:
            return False
        return (Path(settings.data_dir) / clean).is_file()

    def _resolve_media_field(
        raw_media,
        fallback_ext: str,
        message_id: str = "",
        original_filename: str = "",
        original_media_type: str = "",
    ) -> str | None:
        """Resolve media field from webhook payload (path or URL)."""
        if not raw_media:
            return None
        if isinstance(raw_media, str):
            if str(raw_media).startswith(("http://", "https://")):
                downloaded = _download_media_from_url(str(raw_media), fallback_ext=fallback_ext)
                if downloaded:
                    return downloaded
            resolved = _sync_media_to_statics(raw_media)
            if resolved and _media_exists(resolved):
                return resolved
            if message_id:
                downloaded = _download_media_from_message_id(
                    message_id,
                    fallback_ext=fallback_ext,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
                if downloaded:
                    return downloaded
            return resolved
        if isinstance(raw_media, dict):
            path_value = raw_media.get("path", "")
            if path_value:
                resolved = _sync_media_to_statics(path_value)
                if resolved and _media_exists(resolved):
                    return resolved
            url_value = raw_media.get("url", "") or raw_media.get("link", "") or raw_media.get("download_url", "")
            if url_value:
                downloaded = _download_media_from_url(str(url_value), fallback_ext=fallback_ext)
                if downloaded:
                    return downloaded
            if message_id:
                return _download_media_from_message_id(
                    message_id,
                    fallback_ext=fallback_ext,
                    original_filename=(
                        str(raw_media.get("filename", "") or raw_media.get("file_name", "")).strip()
                        or original_filename
                    ),
                    original_media_type=(
                        str(raw_media.get("mimetype", "") or raw_media.get("mime_type", "")).strip()
                        or original_media_type
                    ),
                )
        return None

    # ── Group Mention Helpers ──────────────────────────────────────

    def _is_bot_mentioned(text: str, data: dict) -> bool:
        """Check if the bot is mentioned in a group message."""
        if not text:
            return False
        text_lower = text.lower()
        # Check @phone mention
        bot_phone = state.bot_phone
        if bot_phone and f"@{bot_phone}" in text:
            return True
        # Check @name mention (case-insensitive)
        bot_name = state.bot_name
        if bot_name and f"@{bot_name.lower()}" in text_lower:
            return True
        # Check mentioned_jids from GOWA payload (if present)
        mentioned = data.get("mentioned_jids", data.get("mentioned", []))
        if mentioned and bot_phone:
            for jid in mentioned:
                if bot_phone in str(jid):
                    return True
        return False

    def _strip_bot_mention(text: str) -> str:
        """Remove bot @mention from message text."""
        bot_phone = state.bot_phone
        bot_name = state.bot_name
        if bot_phone:
            text = text.replace(f"@{bot_phone}", "").strip()
        if bot_name:
            text = re.sub(rf"@{re.escape(bot_name)}", "", text, flags=re.IGNORECASE).strip()
        return text

    # ── Reply Splitting & Sending ─────────────────────────────────

    def _parse_split_reply(reply: str) -> list[str]:
        """Parse LLM reply as JSON array of strings. Fallback to single message."""
        text = reply.strip()
        # Strip markdown code block if LLM wraps in ```json ... ```
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]).strip()
        if text.startswith("["):
            try:
                parts = json.loads(text)
                if isinstance(parts, list) and all(isinstance(p, str) for p in parts):
                    filtered = [p.strip() for p in parts if p.strip()]
                    if filtered:
                        return filtered
            except (json.JSONDecodeError, TypeError):
                pass
        return [reply]

    async def _send_reply(phone: str, reply: str):
        """Send reply (possibly split into multiple parts) and broadcast."""
        split_enabled = settings.get("split_messages", True)

        if split_enabled:
            parts = _parse_split_reply(reply)
        else:
            parts = [reply]

        # Initial response delay (simulates typing)
        delay_min = settings.get("response_delay_min", 1.0)
        delay_max = settings.get("response_delay_max", 3.0)
        await asyncio.sleep(random.uniform(delay_min, delay_max))

        sent_parts = []  # collect (part_text, msg_id) for saving after send
        for i, part in enumerate(parts):
            if i > 0:
                # Inter-message delay with ±0.5s variation
                base_delay = settings.get("split_message_delay", 2.0)
                if base_delay > 0:
                    await asyncio.sleep(base_delay + random.uniform(-0.5, 0.5))
                # Re-send typing indicator between parts
                try:
                    await asyncio.to_thread(gowa_client.send_chat_presence, phone)
                except Exception:
                    pass

            # Track for echo-back filtering
            sent_key = f"{phone}:{part[:120]}"
            state.recently_sent[sent_key] = time.time()

            send_result = None
            try:
                send_result = await asyncio.to_thread(gowa_client.send_message, phone, part)
                await atrack_step("gowa_send", {"phone": phone, "part": i + 1, "total_parts": len(parts)})
            except GOWASendError as e:
                logger.error("[Batch] Send failed for %s (part %d/%d): %s", phone, i + 1, len(parts), e)
                await atrack_step("gowa_send", {
                    "phone": phone, "part": i + 1, "error": str(e),
                }, status="error")
                await asyncio.to_thread(gowa_client.stop_chat_presence, phone)
                await ws_manager.broadcast("new_message", {
                    "phone": phone,
                    "message": {"role": "error", "content": f"Falha ao enviar: {e}", "ts": time.time()},
                })
                return

            part_msg_id = extract_msg_id(send_result)
            sent_parts.append((part, part_msg_id))

            # Broadcast each part to frontend individually
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {"role": "assistant", "content": part, "ts": time.time(),
                            "status": "sent", "msg_id": part_msg_id},
            })

        # Save each part as a separate message to preserve split across page refresh
        for part, part_msg_id in sent_parts:
            try:
                await asyncio.to_thread(agent_handler.save_assistant_message, phone, part,
                                        msg_id=part_msg_id, status="sent")
                # Increment unread AI count (operator hasn't seen this reply yet)
                contact = agent_handler._contacts.get(phone)
                if contact:
                    await asyncio.to_thread(contact.increment_unread_ai)
            except Exception as e:
                logger.error("[Batch] Failed to save reply for %s: %s", phone, e)

        await asyncio.to_thread(gowa_client.stop_chat_presence, phone)
        state.msg_count += 1
        full_reply = "\n".join(parts)
        await atrack_step("response_sent", {
            "phone": phone,
            "parts": len(parts),
            "reply_preview": full_reply[:200],
        })
        logger.info("[Batch] Replied to %s (%d parts): %s", phone, len(parts), full_reply[:80])

        await ws_manager.broadcast("status", {
            "connected": state.connected,
            "msg_count": state.msg_count,
            "auto_reply_running": state.auto_reply_running,
            "bot_phone": state.bot_phone,
            "bot_name": state.bot_name,
        })

    async def _broadcast_tool_calls(phone: str, tool_calls: list[dict],
                                    contact_info: dict | None = None):
        """Broadcast private messages for each tool call executed by the LLM."""
        contact = agent_handler._get_contact(phone)
        for tc in tool_calls:
            tool_name = tc.get("tool", "unknown")
            args = tc.get("args", {})
            # Format: tool name + each arg on its own line
            lines = [f"\U0001f527 {tool_name}"]
            for key, value in args.items():
                lines.append(f"{key}: {value}")
            content = "\n".join(lines)

            contact.add_message("tool_call", content)
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": {
                    "role": "tool_call",
                    "content": content,
                    "ts": time.time(),
                },
            })

        # Broadcast updated contact info so the frontend refreshes name/details
        if contact_info:
            logger.info("[ToolCall] Broadcasting contact_info_updated for %s: %s", phone, contact_info)
            await ws_manager.broadcast("contact_info_updated", {
                "phone": phone,
                "info": contact_info,
            })

        # If transfer_to_human was called, broadcast alert + state updates
        if any(tc.get("tool") == "transfer_to_human" for tc in tool_calls):
            await ws_manager.broadcast("human_transfer_alert", {"phone": phone})
            await ws_manager.broadcast("contact_ai_toggled", {
                "phone": phone,
                "ai_enabled": False,
            })
            await ws_manager.broadcast("tags_changed", agent_handler.tag_registry.all())
            await ws_manager.broadcast("contact_tags_updated", {
                "phone": phone,
                "tags": list(contact.tags),
            })

    # Expose broadcast_tool_calls for sandbox route
    deps.broadcast_tool_calls = _broadcast_tool_calls

    # ── Batch Processing ──────────────────────────────────────────

    async def _process_batch(phone: str, delay: float):
        """Wait for batch delay, then process all accumulated messages."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        lock = state.batch_locks.setdefault(phone, asyncio.Lock())
        async with lock:
            items = state.pending_messages.pop(phone, [])
            state.batch_tasks.pop(phone, None)

            if not items:
                return

            # Create execution tracking
            exec_id = await astart_execution(phone, "webhook")

            # Record webhook payload
            await atrack_step("webhook_received", {
                "phone": phone,
                "items": [
                    {k: v for k, v in it.items() if k != "audio_path" or v}
                    for it in items
                ],
            })

            contact = agent_handler._get_contact(phone)

            # Separate plain text items from media items
            text_parts: list[str] = []
            text_msg_ids: list[str] = []
            media_items: list[dict] = []
            for item in items:
                if item.get("image_path") or item.get("audio_path") or item.get("video_path") or item.get("gif_path"):
                    media_items.append(item)
                else:
                    text_parts.append(item.get("text", ""))
                    if item.get("msg_id"):
                        text_msg_ids.append(item["msg_id"])

            await atrack_step("batch_accumulated", {
                "text_count": len(text_parts),
                "media_count": len(media_items),
                "combined_preview": "\n".join(t for t in text_parts if t)[:200],
            })

            # AI is active — mark user messages as read before processing
            # (preserves unread_ai_count so operator sees AI replied)
            if contact.ai_enabled and settings.get("auto_reply", True):
                msg_ids = await asyncio.to_thread(contact.mark_user_messages_as_read)
                if msg_ids:
                    for mid in msg_ids:
                        try:
                            await asyncio.to_thread(gowa_client.mark_as_read, mid, phone)
                        except Exception:
                            pass
                    await ws_manager.broadcast("messages_read", {"phone": phone, "only_user": True})

            # Process combined text messages first
            if text_parts:
                combined = "\n".join(t for t in text_parts if t)
                if combined:
                    logger.info("[Batch] Processing %d text messages from %s: %s",
                                len(text_parts), phone, combined[:80])
                    last_msg_id = text_msg_ids[-1] if text_msg_ids else None
                    contact.add_message("user", combined, msg_id=last_msg_id)
                    if contact.ai_enabled and settings.get("auto_reply", True):
                        if not agent_handler.api_key:
                            notice = "[WhatsBot] API key não configurada."
                            contact.add_message("system_notice", notice)
                            await ws_manager.broadcast("new_message", {
                                "phone": phone,
                                "message": {"role": "system_notice", "content": notice, "ts": time.time()},
                            })
                        else:
                            try:
                                await asyncio.to_thread(gowa_client.send_chat_presence, phone)
                                result = await asyncio.to_thread(
                                    agent_handler.process_message, phone, combined,
                                    save_user_message=False, save_response=False)
                                if result.tool_calls:
                                    await _broadcast_tool_calls(phone, result.tool_calls, result.contact_info)
                                if result.reply:
                                    if result.reply.startswith("[WhatsBot]"):
                                        contact.add_message("system_notice", result.reply)
                                        await ws_manager.broadcast("new_message", {
                                            "phone": phone,
                                            "message": {"role": "system_notice", "content": result.reply, "ts": time.time()},
                                        })
                                    else:
                                        await _send_reply(phone, result.reply)
                            except Exception as e:
                                logger.error("[Batch] Agent error for %s: %s", phone, e)
                                await atrack_step("error", {"error": str(e), "phase": "text_processing"}, status="error")

            # Process each media item individually
            for item in media_items:
                text = item.get("text", "")
                image_path = item.get("image_path")
                audio_path = item.get("audio_path")
                video_path = item.get("video_path")
                gif_path = item.get("gif_path")
                raw_image = item.get("raw_image")
                raw_audio = item.get("raw_audio")
                raw_video = item.get("raw_video")

                # Re-resolve media paths right before processing to mitigate
                # race conditions where GOWA writes files after webhook delivery.
                if image_path:
                    image_path = await asyncio.to_thread(
                        _resolve_media_field,
                        {"path": image_path},
                        "jpg",
                        item.get("msg_id", ""),
                        "",
                        "",
                    )
                elif raw_image:
                    image_path = await asyncio.to_thread(
                        _resolve_media_field,
                        raw_image,
                        "jpg",
                        item.get("msg_id", ""),
                        item.get("original_filename", ""),
                        item.get("original_media_type", ""),
                    )
                if audio_path:
                    audio_path = await asyncio.to_thread(
                        _resolve_media_field,
                        {"path": audio_path},
                        "ogg",
                        item.get("msg_id", ""),
                        "",
                        "",
                    )
                elif raw_audio:
                    audio_path = await asyncio.to_thread(
                        _resolve_media_field,
                        raw_audio,
                        "ogg",
                        item.get("msg_id", ""),
                        item.get("original_filename", ""),
                        item.get("original_media_type", ""),
                    )
                if video_path:
                    video_path = await asyncio.to_thread(
                        _resolve_media_field,
                        {"path": video_path},
                        "mp4",
                        item.get("msg_id", ""),
                        "",
                        "",
                    )
                elif raw_video:
                    video_path = await asyncio.to_thread(
                        _resolve_media_field,
                        raw_video,
                        "mp4",
                        item.get("msg_id", ""),
                        item.get("original_filename", ""),
                        item.get("original_media_type", ""),
                    )
                if gif_path and not gif_path.endswith(".gif"):
                    gif_path = await asyncio.to_thread(
                        _resolve_media_field,
                        {"path": gif_path},
                        "mp4",
                        item.get("msg_id", ""),
                        item.get("original_filename", ""),
                        item.get("original_media_type", ""),
                    )
                elif gif_path:
                    gif_path = await asyncio.to_thread(
                        _resolve_media_field,
                        {"path": gif_path},
                        "gif",
                        item.get("msg_id", ""),
                        item.get("original_filename", ""),
                        item.get("original_media_type", ""),
                    )

                media_label = "image" if image_path else ("audio" if audio_path else ("gif" if gif_path else "video"))
                logger.info("[Batch] Processing %s from %s", media_label, phone)

                # Save message to contact memory
                contact.add_message(
                    "user", text or ("[Áudio recebido]" if audio_path else ("[GIF recebido]" if gif_path else ("[Vídeo recebido]" if video_path else ""))),
                    media_type="image" if image_path else ("audio" if audio_path else ("gif" if gif_path else "video")),
                    media_path=image_path or audio_path or gif_path or video_path,
                    msg_id=item.get("msg_id"),
                )

                # Transcribe audio / describe image
                transcription = ""
                try:
                    if audio_path and settings.get("audio_transcription_enabled", True):
                        transcription = await asyncio.to_thread(
                            agent_handler.transcribe_audio, audio_path, phone)
                    elif image_path and settings.get("image_transcription_enabled", True):
                        transcription = await asyncio.to_thread(
                            agent_handler.describe_image, image_path, phone)
                except Exception as e:
                    logger.error("[Batch] Transcription error for %s: %s", phone, e)

                # Save transcription as private message and broadcast.
                # Also update the original user message so the LLM sees the
                # transcription instead of the placeholder "[Áudio recebido]".
                if transcription:
                    contact.add_message("transcription", transcription)
                    # Update the last user message content with the transcription
                    if audio_path:
                        new_content = f"[Transcrição do áudio]: {transcription}"
                    elif image_path:
                        prefix = f"[Descrição da imagem]: {transcription}"
                        new_content = f"{prefix}\n{text}" if text else prefix
                    else:
                        new_content = None
                    if new_content:
                        await asyncio.to_thread(
                            agent_handler.update_last_user_message_content, phone, new_content
                        )
                    await ws_manager.broadcast("new_message", {
                        "phone": phone,
                        "message": {
                            "role": "transcription",
                            "content": transcription,
                            "ts": time.time(),
                        },
                    })

                if not contact.ai_enabled or not settings.get("auto_reply", True):
                    continue

                if not agent_handler.api_key:
                    notice = "[WhatsBot] API key não configurada."
                    contact.add_message("system_notice", notice)
                    await ws_manager.broadcast("new_message", {
                        "phone": phone,
                        "message": {"role": "system_notice", "content": notice, "ts": time.time()},
                    })
                    continue

                # Build text for LLM: use transcription if available
                llm_text = text or ""
                if audio_path:
                    if transcription:
                        llm_text = f"[Transcrição do áudio]: {transcription}"
                    else:
                        llm_text = llm_text or "[Áudio recebido]"
                elif image_path and transcription:
                    prefix = f"[Descrição da imagem]: {transcription}"
                    llm_text = f"{prefix}\n{text}" if text else prefix
                elif gif_path:
                    llm_text = llm_text or "[GIF recebido]"
                elif video_path:
                    llm_text = llm_text or "[Vídeo recebido]"

                try:
                    await asyncio.to_thread(gowa_client.send_chat_presence, phone)
                    result = await asyncio.to_thread(
                        agent_handler.process_message, phone,
                        llm_text,
                        save_user_message=False, save_response=False,
                        image_path=image_path if (image_path and not transcription) else None,
                    )
                    if result.tool_calls:
                        await _broadcast_tool_calls(phone, result.tool_calls, result.contact_info)
                    if result.reply:
                        if result.reply.startswith("[WhatsBot]"):
                            contact.add_message("system_notice", result.reply)
                            await ws_manager.broadcast("new_message", {
                                "phone": phone,
                                "message": {"role": "system_notice", "content": result.reply, "ts": time.time()},
                            })
                        else:
                            await _send_reply(phone, result.reply)
                except Exception as e:
                    logger.error("[Batch] Agent error for %s (%s): %s", phone, media_label, e)
                    await atrack_step("error", {"error": str(e), "phase": f"{media_label}_processing"}, status="error")

            # Finalize execution as completed
            await aend_execution(exec_id)

            # Prune old executions if beyond limit
            max_exec = settings.get("max_executions", 200)
            try:
                await asyncio.to_thread(prune_executions, max_exec)
            except Exception:
                pass

    # ── Webhook Endpoint ──────────────────────────────────────────

    @app.post("/api/webhook")
    async def webhook(body: dict):
        """Receive real-time message events from GOWA webhook."""
        event = (body.get("event") or body.get("type") or "").strip().lower()
        # GOWA wraps message data inside "payload"
        data = body.get("payload", body.get("data", body))

        # Store raw payload for debugging (last 50, in-memory fallback)
        state.webhook_payloads.append({
            "ts": time.time(),
            "event": event,
            "payload": data,
        })

        # Handle chat presence events (typing/recording indicators)
        if event == "chat_presence":
            from_jid = data.get("from", "")
            phone = from_jid.split("@")[0] if "@" in from_jid else from_jid
            presence_state = data.get("state", "")
            media = data.get("media", "")
            if phone and presence_state:
                logger.info("[Webhook] chat_presence %s from %s (media=%s)",
                            presence_state, phone, media or "text")
                await ws_manager.broadcast("chat_presence", {
                    "phone": phone,
                    "state": presence_state,
                    "media": media,
                })
            return _ok({"status": "presence"})

        # Handle message.ack events (delivery + read receipts from WhatsApp)
        if event == "message.ack":
            receipt_type = (data.get("receipt_type", "") or "").strip().lower()
            msg_ids = data.get("ids", [])

            # Extract phone from ack payload (try multiple fields, GOWA is inconsistent)
            ack_phone = ""
            for field in ("chat_id", "from", "jid", "phone"):
                val = data.get(field, "")
                if val and "@" in val:
                    ack_phone = val.split("@")[0]
                    break
                elif val and not ack_phone:
                    ack_phone = val

            # Fallback: look up phone from the message in DB
            if not ack_phone and msg_ids:
                cid = await asyncio.to_thread(message_repo.get_contact_id_by_msg_id, msg_ids[0])
                if cid:
                    db_contact = await asyncio.to_thread(contact_repo.get_by_id, cid)
                    if db_contact:
                        ack_phone = db_contact["phone"]
                if cid and not ack_phone:
                    for phone_key, contact in agent_handler._contacts.items():
                        if contact.id == cid:
                            ack_phone = phone_key
                            break

            if receipt_type == "delivered" and msg_ids:
                # Update outgoing message status to "delivered" (with cascade to prior msgs)
                all_updated = []
                for mid in msg_ids:
                    updated = await asyncio.to_thread(message_repo.update_status_by_msg_id, mid, "delivered")
                    all_updated.extend(updated)
                # Deduplicate
                all_updated = list(dict.fromkeys(all_updated))
                logger.info("[Webhook] message.ack delivered for %s (ids=%s, cascaded=%d)",
                            ack_phone, msg_ids, len(all_updated))
                if ack_phone and all_updated:
                    await ws_manager.broadcast("message_status", {
                        "phone": ack_phone,
                        "msg_ids": all_updated,
                        "status": "delivered",
                    })

            elif receipt_type in ("read", "read-self") and msg_ids:
                # Update outgoing message status to "read" (with cascade to prior msgs)
                all_updated = []
                for mid in msg_ids:
                    updated = await asyncio.to_thread(message_repo.update_status_by_msg_id, mid, "read")
                    all_updated.extend(updated)
                all_updated = list(dict.fromkeys(all_updated))
                logger.info("[Webhook] message.ack read for %s (ids=%s, cascaded=%d)",
                            ack_phone, msg_ids, len(all_updated))
                if ack_phone and all_updated:
                    await ws_manager.broadcast("message_status", {
                        "phone": ack_phone,
                        "msg_ids": all_updated,
                        "status": "read",
                    })

                # Existing unread tracking logic (for incoming messages read by us)
                for phone_key, contact in agent_handler._contacts.items():
                    unread_ids = contact.get_unread_msg_ids()
                    matched = [mid for mid in msg_ids if mid in unread_ids]
                    if matched:
                        logger.info("[Webhook] message.ack unread cleared for %s (ids=%s)", phone_key, matched)
                        contact.mark_as_read()
                        await ws_manager.broadcast("messages_read", {"phone": phone_key})

            return _ok({"status": "ack"})

        # Only process incoming messages
        if event and event not in ("message", "message:received", ""):
            return _ok({"status": "ignored"})

        if not isinstance(data, dict):
            return _ok({"status": "ignored"})

        # Extract message fields (GOWA field names vary)
        is_from_me = data.get("is_from_me", data.get("from_me", data.get("FromMe", False)))

        # Capture bot's own phone from outgoing messages (for @mention detection)
        if is_from_me:
            own_jid = (data.get("sender_jid", "") or data.get("from", "")
                       or data.get("sender", ""))
            if own_jid and "@s.whatsapp.net" in own_jid:
                state.bot_phone = own_jid.split("@")[0].split(":")[0]
                logger.info("[Webhook] Bot phone captured from own message: %s", state.bot_phone)

        msg_id = data.get("id", data.get("Id", data.get("message_id", ""))
                         ) or str(uuid.uuid4())
        if msg_id in state.processed_messages:
            return _ok({"status": "duplicate"})

        # Extract body — try multiple known field names
        text = (data.get("content", "")
                or data.get("body", "")
                or data.get("Body", "")
                or data.get("message", "")
                or data.get("text", "")).strip()

        # Extract media paths from GOWA payload
        image_path: str | None = None
        audio_path: str | None = None
        gif_path: str | None = None
        video_path: str | None = None

        raw_image = data.get("image")
        original_filename = str(data.get("original_filename", "")).strip()
        original_media_type = str(data.get("original_media_type", "")).strip()
        has_image_payload = bool(raw_image)
        has_audio_payload = bool(data.get("audio"))
        has_video_payload = bool(data.get("video"))
        if raw_image:
            if isinstance(raw_image, str):
                image_path = _resolve_media_field(
                    raw_image, "jpg", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
            elif isinstance(raw_image, dict):
                image_path = _resolve_media_field(
                    raw_image, "jpg", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
                if not text:
                    text = (raw_image.get("caption", "") or "").strip()

        raw_audio = data.get("audio")
        if raw_audio:
            if isinstance(raw_audio, str):
                audio_path = _resolve_media_field(
                    raw_audio, "ogg", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
            elif isinstance(raw_audio, dict):
                audio_path = _resolve_media_field(
                    raw_audio, "ogg", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )

        # WhatsApp GIFs may arrive under "video" with gif flags/mime.
        raw_video = data.get("video")
        if raw_video:
            if isinstance(raw_video, str):
                candidate = _resolve_media_field(
                    raw_video, "mp4", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
                if candidate and candidate.lower().endswith(".gif"):
                    gif_path = candidate
                elif candidate:
                    video_path = candidate
            elif isinstance(raw_video, dict):
                candidate = _resolve_media_field(
                    raw_video, "mp4", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
                mime = str(raw_video.get("mimetype", raw_video.get("mime_type", ""))).lower()
                is_gif = bool(
                    raw_video.get("is_gif")
                    or raw_video.get("gif_playback")
                    or "gif" in mime
                    or (candidate and candidate.lower().endswith(".gif"))
                )
                if is_gif:
                    gif_path = candidate
                elif candidate:
                    video_path = candidate
                if not text:
                    text = (raw_video.get("caption", "") or "").strip()

        # Keep GIF explicit (can be .gif image or short mp4 loop flagged as gif).

        # Video notes (voice messages) are treated as audio
        raw_vn = data.get("video_note")
        if raw_vn and not audio_path:
            if isinstance(raw_vn, str):
                audio_path = _resolve_media_field(
                    raw_vn, "mp4", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )
            elif isinstance(raw_vn, dict):
                audio_path = _resolve_media_field(
                    raw_vn, "mp4", message_id=msg_id,
                    original_filename=original_filename,
                    original_media_type=original_media_type,
                )

        # For audio without text, set a placeholder
        if audio_path and not text:
            text = "[Áudio recebido]" if not is_from_me else "[Áudio enviado]"

        # For image without text, set a placeholder for outgoing
        if image_path and not text and is_from_me:
            text = "[Imagem enviada]"

        # Extract chat and sender separately for group support
        chat_jid = (data.get("chat_jid", "") or data.get("chat_id", "")
                    or data.get("from", "") or data.get("jid", ""))
        sender_jid = data.get("sender_jid", "") or data.get("sender", "")

        is_group = "@g.us" in chat_jid
        is_channel = any(tag in chat_jid for tag in ("@newsletter", "@broadcast"))

        # Ignore channels/newsletters/broadcasts. The panel should show only
        # private chats and groups.
        if is_channel:
            logger.info("[Webhook] Ignoring channel/broadcast message from chat_jid=%s", chat_jid)
            return _ok({"status": "ignored_channel"})

        if is_group:
            # For groups: route replies to the group, track individual sender
            phone = chat_jid  # keep full JID (e.g. 120363xxx@g.us)
            individual_phone = sender_jid.split("@")[0] if "@" in sender_jid else sender_jid
            from_name = data.get("from_name", "") or data.get("pushName", "") or data.get("notify", "")
        else:
            # For private chats: use sender as before
            sender = sender_jid or chat_jid
            phone = sender.split("@")[0] if "@" in sender else sender
            individual_phone = phone
            from_name = data.get("from_name", "") or data.get("pushName", "") or data.get("notify", "")

        if not phone or (
            not text
            and not image_path and not audio_path and not video_path
            and not has_image_payload and not has_audio_payload and not has_video_payload
        ):
            logger.info("[Webhook] Skipping: text=%r phone=%r media=%s",
                        text[:50] if text else "", phone,
                        "image" if image_path else ("audio" if audio_path else ("video" if video_path else "none")))
            return _ok({"status": "ignored"})

        state.processed_messages.add(msg_id)

        # Filter GOWA echo-backs: ignore messages we recently sent
        if text:
            sent_key = f"{phone}:{text[:120]}"
            sent_at = state.recently_sent.pop(sent_key, None)
            if sent_at and (time.time() - sent_at) < 30:
                logger.info("[Webhook] Ignoring echo-back for %s", phone)
                return _ok({"status": "echo"})

        # Sync outgoing messages sent from phone (not via our app)
        if is_from_me:
            # Determine media metadata
            media_type: str | None = None
            media_path: str | None = None
            if image_path:
                media_type = "image"
                media_path = image_path
            elif audio_path:
                media_type = "audio"
                media_path = audio_path
            elif gif_path:
                media_type = "gif"
                media_path = gif_path
            elif video_path:
                media_type = "video"
                media_path = video_path

            logger.info("[Webhook] Syncing outgoing %s to %s: %s",
                        media_type or "message", phone,
                        text[:80] if text else f"[{media_type}]")

            # Save as "assistant" in contact memory (status="operator" to distinguish from AI)
            contact = agent_handler._get_contact(phone)
            await asyncio.to_thread(
                contact.add_message, "assistant", text,
                media_type=media_type, media_path=media_path, msg_id=msg_id,
                status="operator")

            # Broadcast to frontend
            broadcast_msg: dict = {"role": "assistant", "content": text,
                                   "ts": time.time(), "msg_id": msg_id}
            if media_type:
                broadcast_msg["media_type"] = media_type
                broadcast_msg["media_path"] = media_path
            await ws_manager.broadcast("new_message", {
                "phone": phone,
                "message": broadcast_msg,
            })

            return _ok({"status": "synced"})

        # Determine media metadata for broadcast
        media_type: str | None = None
        media_path: str | None = None
        if image_path:
            media_type = "image"
            media_path = image_path
        elif audio_path:
            media_type = "audio"
            media_path = audio_path
        elif gif_path:
            media_type = "gif"
            media_path = gif_path
        elif video_path:
            media_type = "video"
            media_path = video_path

        # For groups: prefix text with sender name and check @mention
        display_text = text
        skip_ai = False
        if is_group:
            # Log full group payload for debugging field names
            logger.info("[Webhook] Group payload: %s", json.dumps(data, default=str, ensure_ascii=False)[:2000])

            # Ensure group metadata is stored
            contact = agent_handler._get_contact(phone)
            if not contact.is_group or not contact.group_name:
                contact.is_group = True
                # Try to get group name from payload fields (NOT from_name, that's the sender)
                group_name = (data.get("subject", "")
                              or data.get("group_name", "")
                              or data.get("group_subject", "")
                              or data.get("chat_name", ""))
                # Fallback: fetch group name from GOWA API
                if not group_name:
                    try:
                        group_name = await asyncio.to_thread(
                            gowa_client.get_group_name, phone)
                    except Exception as e:
                        logger.warning("[Webhook] Failed to fetch group name: %s", e)
                if group_name:
                    contact.group_name = group_name
                    logger.info("[Webhook] Group name resolved: %s -> %s", phone, group_name)
                else:
                    logger.warning("[Webhook] Could not resolve group name for %s", phone)
                contact.save()

            # Check if bot can send in this group
            if state.bot_phone:
                try:
                    can_send = await asyncio.to_thread(
                        gowa_client.can_bot_send_in_group, phone, state.bot_phone)
                    if contact.can_send != can_send:
                        contact.can_send = can_send
                        contact.save()
                        logger.info("[Webhook] Group %s can_send updated: %s", phone, can_send)
                except Exception as e:
                    logger.warning("[Webhook] Failed to check group send permission: %s", e)

            # Prefix message with sender name for group context
            sender_label = from_name or individual_phone
            if text:
                display_text = f"[{sender_label}]: {text}"

            # Check if bot is mentioned
            group_mode = settings.get("group_reply_mode", "mention_only")
            bot_mentioned = _is_bot_mentioned(text, data)

            if group_mode == "never" or (group_mode == "mention_only" and not bot_mentioned):
                skip_ai = True
                logger.info("[Webhook] Group message (no mention) from %s in %s: %s",
                            sender_label, phone, text[:80] if text else "[media]")
            else:
                # Bot was mentioned — strip mention from text for LLM
                cleaned = _strip_bot_mention(text)
                display_text = f"[{sender_label}]: {cleaned}" if cleaned else display_text
                logger.info("[Webhook] Group message (@mention) from %s in %s: %s",
                            sender_label, phone, text[:80] if text else "[media]")
        else:
            logger.info("[Webhook] %s from %s: %s",
                        media_type.capitalize() if media_type else "Message",
                        phone, text[:80] if text else f"[{media_type}]")

        # Check/update archive status from GOWA (skip if archived by app)
        try:
            contact = agent_handler._get_contact(phone)
            if not contact.archived_by_app:
                archived = await asyncio.to_thread(gowa_client.is_chat_archived, chat_jid)
                logger.info("[Webhook] Archive check: %s (jid=%s) -> archived=%s", phone, chat_jid, archived)
                if contact.is_archived != archived:
                    contact.is_archived = archived
                    contact.save()
                    logger.info("[Webhook] Archive status updated: %s -> %s", phone, archived)
            else:
                logger.info("[Webhook] Skipping archive check for %s (archived by app)", phone)
        except Exception as e:
            logger.warning("[Webhook] Failed to check archive status for %s: %s", phone, e)

        # Auto-fill contact name from WhatsApp pushName (private chats only)
        # Some payloads arrive without from_name; fallback to user/info API.
        if not from_name and not is_group:
            try:
                from_name = await asyncio.to_thread(
                    gowa_client.get_contact_name,
                    sender_jid or phone,
                )
            except Exception:
                from_name = ""
        if from_name and not is_group:
            await asyncio.to_thread(agent_handler._get_contact(phone).set_wa_name, from_name)

        # Increment unread count for incoming user messages
        await asyncio.to_thread(lambda: agent_handler._get_contact(phone).increment_unread(msg_id))

        # Broadcast incoming message to frontend in real-time
        broadcast_msg: dict = {"role": "user", "content": display_text, "ts": time.time(), "msg_id": msg_id}
        if media_type:
            broadcast_msg["media_type"] = media_type
            broadcast_msg["media_path"] = media_path
        await ws_manager.broadcast("new_message", {
            "phone": phone,
            "message": broadcast_msg,
        })

        # For group messages without mention: save to history but don't trigger AI
        if skip_ai:
            await asyncio.to_thread(
                agent_handler._get_contact(phone).add_message,
                "user", display_text, msg_id=msg_id)
            return _ok({"status": "group_no_mention"})

        # Batch messages — accumulate and wait before responding
        if phone not in state.pending_messages:
            state.pending_messages[phone] = []
        state.pending_messages[phone].append({
            "text": display_text,
            "image_path": image_path,
            "audio_path": audio_path,
            "gif_path": gif_path,
            "video_path": video_path,
            "raw_image": raw_image,
            "raw_audio": raw_audio,
            "raw_video": raw_video,
            "original_filename": original_filename,
            "original_media_type": original_media_type,
            "msg_id": msg_id,
        })

        # Cancel existing batch timer for this contact
        if phone in state.batch_tasks:
            state.batch_tasks[phone].cancel()

        # Schedule batch processing after delay
        batch_delay = settings.get("message_batch_delay", 3.0)
        state.batch_tasks[phone] = asyncio.create_task(
            _process_batch(phone, batch_delay)
        )

        # Prune processed set to avoid unbounded growth
        if len(state.processed_messages) > 5000:
            oldest = list(state.processed_messages)[:2500]
            for item in oldest:
                state.processed_messages.discard(item)

        # Prune stale recently_sent entries (older than 60s)
        now = time.time()
        stale = [k for k, v in state.recently_sent.items() if now - v > 60]
        for k in stale:
            del state.recently_sent[k]

        return _ok({"status": "batched"})
