import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// ── Моки API-клиента ───────────────────────────────────────────────
// devLogin/magicLink резолвятся не сразу: возвращаем «висящий» промис,
// чтобы проверить, что URL чистится ДО сетевого ответа (не после nav()).
const mockApi = vi.hoisted(() => ({
  devLogin: vi.fn(),
  magicLink: vi.fn(),
  telegramLogin: vi.fn(),
  login: vi.fn(),
  authConfig: vi.fn(),
}));
vi.mock("@/api/client", () => ({ api: mockApi }));

// useAuth — LoginPage берёт из него только refresh().
const mockRefresh = vi.hoisted(() => vi.fn());
vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ refresh: mockRefresh }),
}));

import LoginPage from "@/routes/LoginPage";

// Утилита: подменяем search в jsdom-окне и рендерим LoginPage в роутере,
// инициализированном тем же URL.
function renderWithUrl(search: string) {
  window.history.replaceState({}, "", `/login${search}`);
  return render(
    <MemoryRouter initialEntries={[`/login${search}`]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe("LoginPage: токен не утекает в Referer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefresh.mockResolvedValue(undefined);
    // «Висящий» промис — логин не завершается во время проверки URL.
    mockApi.devLogin.mockReturnValue(new Promise(() => {}));
    mockApi.magicLink.mockReturnValue(new Promise(() => {}));
    // Бот подключён — виджет было бы из чего собрать; проверяем, что при
    // секрете в URL его всё равно не грузят.
    mockApi.authConfig.mockResolvedValue({
      telegram_enabled: true,
      telegram_bot_username: "velsbot",
    });
  });

  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("dev-token: убирает ?token= из URL сразу на маунте (до ответа сети)", async () => {
    renderWithUrl("?token=SECRET123");

    // URL должен очиститься синхронно/в момент маунта, ещё ДО резолва devLogin.
    await waitFor(() => {
      expect(window.location.search).not.toContain("SECRET123");
    });
    expect(window.location.href).not.toContain("SECRET123");

    // При этом токен передан в обработчик логина.
    expect(mockApi.devLogin).toHaveBeenCalledWith("SECRET123");
  });

  it("magic: убирает ?magic= из URL сразу на маунте (до ответа сети)", async () => {
    renderWithUrl("?magic=MAGICSECRET");

    await waitFor(() => {
      expect(window.location.search).not.toContain("MAGICSECRET");
    });
    expect(window.location.href).not.toContain("MAGICSECRET");
    expect(mockApi.magicLink).toHaveBeenCalledWith("MAGICSECRET");
  });

  it("не создаёт внешний telegram-widget-скрипт при наличии токена в URL", async () => {
    // При наличии секрета в URL внешний скрипт telegram.org НЕ должен
    // создаваться — иначе браузер ушлёт Referer с токеном на третью сторону.
    const createElementSpy = vi.spyOn(document, "createElement");

    renderWithUrl("?token=SECRET123");

    await waitFor(() => {
      expect(window.location.search).not.toContain("SECRET123");
    });
    const telegramScriptCreated = createElementSpy.mock.results.some((r) => {
      const el = r.value as HTMLElement | undefined;
      return el instanceof HTMLScriptElement && el.src.includes("telegram.org");
    });
    expect(telegramScriptCreated).toBe(false);

    createElementSpy.mockRestore();
  });
});
