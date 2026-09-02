// WebSocket client with auto-reconnect (exponential backoff capped at 30s).
// Reconnect-recovery via REST ?since= happens in Chat.tsx via the onOpen
// callback (added in Stage 9).

import type { WsMessage } from "@/lib/types";
import { BASE_WITH_SLASH } from "@/api/base";
import { AUTH_UNAUTHORIZED_EVENT } from "@/api/client";

// Код закрытия, который шлёт сервер при отказе по политике/авторизации
// (протухшая/отозванная кука, снятие из whitelist, отключённый аккаунт) —
// см. routes_ws.py (WS_1008_POLICY_VIOLATION).
const WS_POLICY_VIOLATION = 1008;

export interface WsHandle {
  send: (msg: object) => void;
  close: () => void;
}

export interface WsOpts {
  onOpen?: () => void | Promise<void>;
  onClose?: () => void;
}

export function openSessionWs(
  sessionUuid: string,
  onMessage: (msg: WsMessage) => void,
  opts: WsOpts = {}
): WsHandle {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  let ws: WebSocket | null = null;
  let closedByUser = false;
  let backoff = 500;

  const connect = () => {
    ws = new WebSocket(`${proto}//${window.location.host}${BASE_WITH_SLASH}api/ws/sessions/${sessionUuid}`);
    ws.onopen = () => {
      backoff = 500;
      void opts.onOpen?.();
    };
    ws.onmessage = (e) => {
      try {
        onMessage(JSON.parse(e.data) as WsMessage);
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = (e) => {
      opts.onClose?.();
      if (closedByUser) return;
      // Отзыв/протухание сессии: сервер закрывает handshake кодом 1008.
      // Реконнект бессмысленен (упрётся в тот же 1008), а сам канал —
      // единственный признак для UI, что доступ пропал. Шлём глобальный
      // сигнал разлогина (AuthProvider → ProtectedRoute уводит на /login)
      // и прекращаем реконнект. Без этого отозванный юзер вечно долбился бы
      // в закрытый сокет, а logout зависел бы от побочного REST-поллинга.
      if (e.code === WS_POLICY_VIOLATION) {
        closedByUser = true;
        if (typeof window !== "undefined") {
          window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
        }
        return;
      }
      setTimeout(connect, Math.min(backoff, 30000));
      backoff *= 2;
    };
    ws.onerror = () => {
      /* onclose will follow */
    };
  };
  connect();

  return {
    send: (msg) => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(msg));
      }
    },
    close: () => {
      closedByUser = true;
      ws?.close();
    },
  };
}
