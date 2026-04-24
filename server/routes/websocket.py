"""WebSocket endpoint."""

import json
import os

from fastapi import WebSocket, WebSocketDisconnect

from server.auth import auth_required, verify_token


def register_routes(app, deps):
    ws_manager = deps.ws_manager
    state = deps.state
    settings = deps.settings

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        reset_db_token = None
        reset_slug_token = None

        # In SaaS mode, HTTP middleware does not run for WebSocket connections,
        # so we must resolve tenant context directly from the websocket host.
        registry = getattr(deps, "_registry", None)
        if registry is not None:
            from server.middleware import RESERVED_SUBDOMAINS
            from server.tenant import current_tenant_db, current_tenant_slug

            forwarded_host = websocket.headers.get("x-forwarded-host", "")
            if forwarded_host:
                host = forwarded_host.split(",")[0].strip().split(":")[0].lower()
            else:
                host = websocket.headers.get("host", "").split(":")[0].lower()

            base_domain = os.environ.get("WHATSBOT_DOMAIN", "localhost").lower()
            slug = ""
            if host == f"admin.{base_domain}" or host == "admin" or host.startswith("admin."):
                await websocket.accept()
                await websocket.close(code=4403, reason="Admin host does not support tenant websocket")
                return

            if host.endswith(f".{base_domain}"):
                slug = host[: -(len(base_domain) + 1)]
            elif "." in host:
                first_label = host.split(".", 1)[0]
                if first_label not in RESERVED_SUBDOMAINS:
                    slug = first_label

            if not slug:
                await websocket.accept()
                await websocket.close(code=4404, reason="Tenant not resolved from host")
                return

            ctx = registry.get_by_slug(slug)
            if not ctx:
                await websocket.accept()
                await websocket.close(code=4404, reason="Tenant not found")
                return

            reset_slug_token = current_tenant_slug.set(slug)
            reset_db_token = current_tenant_db.set(ctx.db_name)

        # Check auth token from query param if password is set
        if auth_required(settings):
            token = websocket.query_params.get("token", "")
            if not token or not verify_token(token, settings):
                await websocket.accept()
                await websocket.close(code=4401, reason="Unauthorized")
                return

        await ws_manager.connect(websocket)
        # Send initial state
        try:
            await websocket.send_text(json.dumps({"event": "status", "data": {
                "connected": state.connected,
                "msg_count": state.msg_count,
                "auto_reply_running": state.auto_reply_running,
            }}))
            await websocket.send_text(json.dumps({"event": "gowa_status", "data": {
                "message": state.notification,
            }}))
            # Send current QR state so page refreshes show QR immediately
            if not state.connected and state.qr_data:
                await websocket.send_text(json.dumps({"event": "qr_update", "data": {
                    "available": True,
                    "version": state.qr_version,
                }}))
            else:
                await websocket.send_text(json.dumps({"event": "qr_update", "data": {
                    "available": False,
                }}))
        except Exception:
            pass
        # Keep alive
        try:
            while True:
                data = await websocket.receive_text()
                msg = json.loads(data)
                if msg.get("action") == "ping":
                    await websocket.send_text(json.dumps({"event": "pong", "data": {}}))
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)
        except Exception:
            ws_manager.disconnect(websocket)
        finally:
            if reset_db_token is not None:
                from server.tenant import current_tenant_db
                current_tenant_db.reset(reset_db_token)
            if reset_slug_token is not None:
                from server.tenant import current_tenant_slug
                current_tenant_slug.reset(reset_slug_token)
