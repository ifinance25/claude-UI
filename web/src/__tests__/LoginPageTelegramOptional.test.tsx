import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// В light Telegram НЕОБЯЗАТЕЛЕН: установщик разрешает пропустить токен, и тогда
// systemd поднимает только веб. Страница входа не должна предлагать войти через
// бота, которого нет, — признак приходит с сервера (/api/auth/config).
const mockApi = vi.hoisted(() => ({
  devLogin: vi.fn(),
  magicLink: vi.fn(),
  telegramLogin: vi.fn(),
  login: vi.fn(),
  authConfig: vi.fn(),
}));
vi.mock("@/api/client", () => ({ api: mockApi }));

const mockRefresh = vi.hoisted(() => vi.fn());
vi.mock("@/auth/AuthContext", () => ({
  useAuth: () => ({ refresh: mockRefresh }),
}));

import LoginPage from "@/routes/LoginPage";

function renderLogin(search = "") {
  window.history.replaceState({}, "", `/login${search}`);
  return render(
    <MemoryRouter initialEntries={[`/login${search}`]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

function telegramWidgetScript(): HTMLScriptElement | null {
  return document.querySelector<HTMLScriptElement>('script[src*="telegram.org"]');
}

describe("LoginPage: Telegram-блоки зависят от того, подключён ли бот", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefresh.mockResolvedValue(undefined);
  });

  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("без бота не упоминает Telegram и не грузит виджет", async () => {
    mockApi.authConfig.mockResolvedValue({
      telegram_enabled: false,
      telegram_bot_username: "",
    });

    renderLogin();
    await screen.findByRole("button", { name: "Войти" });

    // Ни подзаголовка «через Telegram», ни разделителя, ни подсказки /weblogin.
    await waitFor(() => expect(screen.queryByText(/Telegram/i)).toBeNull());
    expect(screen.queryByText("/weblogin")).toBeNull();
    expect(telegramWidgetScript()).toBeNull();

    // Вход по логину/паролю — единственный путь, он на месте.
    expect(screen.getByPlaceholderText("Логин")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Пароль")).toBeInTheDocument();
  });

  it("с ботом показывает виджет и подсказку /weblogin", async () => {
    mockApi.authConfig.mockResolvedValue({
      telegram_enabled: true,
      telegram_bot_username: "velsbot",
    });

    renderLogin();

    expect(
      await screen.findByText("Войдите, чтобы продолжить"),
    ).toBeInTheDocument();
    expect(screen.getByText("или через Telegram")).toBeInTheDocument();
    expect(screen.getByText("/weblogin")).toBeInTheDocument();
    await waitFor(() => {
      const script = telegramWidgetScript();
      expect(script).not.toBeNull();
      expect(script?.getAttribute("data-telegram-login")).toBe("velsbot");
    });
  });

  it("молчит про Telegram, если конфиг не ответил", async () => {
    // Сервер не отдал конфиг — показывать блоки «на всякий случай» хуже, чем
    // не показать: вход по логину/паролю работает в любой установке.
    mockApi.authConfig.mockRejectedValue(new Error("500"));

    renderLogin();
    await screen.findByRole("button", { name: "Войти" });

    await waitFor(() => expect(mockApi.authConfig).toHaveBeenCalled());
    expect(screen.queryByText(/Telegram/i)).toBeNull();
    expect(telegramWidgetScript()).toBeNull();
  });

  it("без бота не советует запросить magic-ссылку у бота", async () => {
    // Протухшая ссылка из старой установки: бота уже нет, отправлять человека
    // к /weblogin некуда.
    mockApi.authConfig.mockResolvedValue({
      telegram_enabled: false,
      telegram_bot_username: "",
    });
    mockApi.magicLink.mockRejectedValue(new Error("401"));

    renderLogin("?magic=STALE");

    const box = await screen.findByText(/Не удалось войти по magic-ссылке/);
    await waitFor(() => expect(mockApi.authConfig).toHaveBeenCalled());
    expect(box.textContent).not.toContain("/weblogin");
  });

  it("с ботом та же ошибка подсказывает /weblogin", async () => {
    mockApi.authConfig.mockResolvedValue({
      telegram_enabled: true,
      telegram_bot_username: "velsbot",
    });
    mockApi.magicLink.mockRejectedValue(new Error("401"));

    renderLogin("?magic=STALE");

    const box = await screen.findByText(/Не удалось войти по magic-ссылке/);
    await waitFor(() => expect(box.textContent).toContain("/weblogin"));
  });
});
