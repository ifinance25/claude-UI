// Отзыв/протухание сессии на WebSocket-канале: сервер закрывает handshake
// кодом 1008 (POLICY_VIOLATION). Клиент должен разлогинить (auth:unauthorized)
// и прекратить реконнект, а не долбиться вечно в закрытый сокет.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

class FakeWS {
  static instances: FakeWS[] = [];
  onopen: ((e?: unknown) => void) | null = null;
  onmessage: ((e: unknown) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onerror: ((e?: unknown) => void) | null = null;
  readyState = 0;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWS.instances.push(this);
  }
  send() {}
  close() {}
}

describe("openSessionWs — отзыв сессии", () => {
  beforeEach(() => {
    FakeWS.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeWS);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("на 1008 диспатчит auth:unauthorized и НЕ реконнектит", async () => {
    const { openSessionWs } = await import("@/api/ws");
    const { AUTH_UNAUTHORIZED_EVENT } = await import("@/api/client");
    const handler = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler);

    openSessionWs("uuid", () => {});
    expect(FakeWS.instances).toHaveLength(1);

    FakeWS.instances[0].onclose!({ code: 1008 });
    expect(handler).toHaveBeenCalledTimes(1);

    // Реконнекта быть не должно даже после прогона всех таймеров.
    vi.advanceTimersByTime(60000);
    expect(FakeWS.instances).toHaveLength(1);

    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler);
  });

  it("на обычном обрыве (1006) реконнектит и НЕ разлогинивает", async () => {
    const { openSessionWs } = await import("@/api/ws");
    const { AUTH_UNAUTHORIZED_EVENT } = await import("@/api/client");
    const handler = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler);

    openSessionWs("uuid", () => {});
    FakeWS.instances[0].onclose!({ code: 1006 });
    expect(handler).not.toHaveBeenCalled();

    // backoff 500ms → новый сокет.
    vi.advanceTimersByTime(600);
    expect(FakeWS.instances).toHaveLength(2);

    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler);
  });

  it("закрытие самим пользователем не реконнектит и не разлогинивает", async () => {
    const { openSessionWs } = await import("@/api/ws");
    const { AUTH_UNAUTHORIZED_EVENT } = await import("@/api/client");
    const handler = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handler);

    const h = openSessionWs("uuid", () => {});
    h.close();
    FakeWS.instances[0].onclose!({ code: 1000 });
    vi.advanceTimersByTime(60000);

    expect(handler).not.toHaveBeenCalled();
    expect(FakeWS.instances).toHaveLength(1);

    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handler);
  });
});
