import { useEffect } from 'preact/hooks';
import { createWebSocket } from '../services/websocket.js';

/**
 * @param {{ current: Record<string, Function | undefined> }} handlersRef
 *        Stable ref; assign handlersRef.current each render so WS callbacks
 *        always invoke the latest logic (config, state setters, etc.).
 */
export function useWebSocket(handlersRef) {
  useEffect(() => {
    const h = () => handlersRef.current || {};
    const ws = createWebSocket({
      onConnect: () => h().onWsConnect?.(),
      onDisconnect: () => h().onWsDisconnect?.(),
      status: (data) => h().onStatus?.(data),
      qr_update: (data) => h().onQrUpdate?.(data),
      gowa_status: (data) => h().onGowaStatus?.(data),
      config_saved: () => h().onConfigSaved?.(),
      new_message: (data) => h().onNewMessage?.(data),
      chat_presence: (data) => h().onChatPresence?.(data),
      contact_info_updated: (data) => h().onContactInfoUpdated?.(data),
      tags_changed: (data) => h().onTagsChanged?.(data),
      contact_tags_updated: (data) => h().onContactTagsUpdated?.(data),
      human_transfer_alert: () => h().onHumanTransferAlert?.(),
      contact_ai_toggled: (data) => h().onContactAiToggled?.(data),
      messages_read: (data) => h().onMessagesRead?.(data),
      message_status: (data) => h().onMessageStatus?.(data),
    });
    return () => ws.close();
  }, [handlersRef]);
}
