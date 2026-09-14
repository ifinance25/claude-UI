import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { SparklesIcon } from "@/components/icons";

declare global {
  interface Window {
    onTelegramAuth: (user: Record<string, unknown>) => void;
  }
}

const BOT_USERNAME = import.meta.env.VITE_BOT_USERNAME ?? "";

/**
 * Снимает секреты входа (`token`/`magic`) из адресной строки браузера ДО
 * того как страница успеет отрендерить внешний telegram-widget или сделать
 * сетевой запрос. Иначе браузер отправит полный URL (с токеном) в заголовке
 * `Referer` на telegram.org — утечка долгоживущего dev-bearer = полный вход.
 *
 * Выполняется один раз, на этапе инициализации модуля/компонента, синхронно
 * через `window.history.replaceState`. Возвращает захваченные значения, чтобы
 * логин-флоу мог их использовать уже после очистки URL.
 *
 * SSR/без window — безопасный no-op.
 */
function consumeAuthSecretsFromUrl(): { token: string | null; magic: string | null } {
  if (typeof window === "undefined") return { token: null, magic: null };
  const url = new URL(window.location.href);
  const token = url.searchParams.get("token");
  const magic = url.searchParams.get("magic");
  if (!token && !magic) return { token: null, magic: null };
  url.searchParams.delete("token");
  url.searchParams.delete("magic");
  // replaceState не триггерит навигацию/перерисовку и не оставляет токен в
  // history — браузер с этого момента не положит его в Referer.
  window.history.replaceState(
    window.history.state,
    "",
    `${url.pathname}${url.search}${url.hash}`,
  );
  return { token, magic };
}

export default function LoginPage() {
  const widgetRef = useRef<HTMLDivElement>(null);
  const nav = useNavigate();
  const { refresh } = useAuth();
  // Захватываем и СРАЗУ стираем секреты из URL на первом рендере (до effect'ов,
  // до загрузки telegram-widget, до любого fetch). useRef-инициализатор
  // выполняется ровно один раз за жизнь компонента.
  const secretsRef = useRef<{ token: string | null; magic: string | null } | null>(
    null,
  );
  if (secretsRef.current === null) {
    secretsRef.current = consumeAuthSecretsFromUrl();
  }
  const { token: capturedToken, magic: capturedMagic } = secretsRef.current;
  const [error, setError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const consumedMagicRef = useRef<string | null>(null);
  const consumedTokenRef = useRef<string | null>(null);

  const submitLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.login(username.trim(), password);
      await refresh();
      nav("/", { replace: true });
    } catch (err) {
      setError(`Не удалось войти: ${(err as Error).message}`);
    } finally {
      setSubmitting(false);
    }
  };

  useEffect(() => {
    const magic = capturedMagic;
    if (!magic) return;
    if (consumedMagicRef.current === magic) return;
    consumedMagicRef.current = magic;
    (async () => {
      try {
        await api.magicLink(magic);
        await refresh();
        nav("/", { replace: true });
      } catch (e) {
        setError(
          `Не удалось войти по magic-ссылке: ${(e as Error).message}. ` +
            "Запросите свежую ссылку через /weblogin в боте."
        );
      }
    })();
  }, [capturedMagic, nav, refresh]);

  useEffect(() => {
    const token = capturedToken;
    if (!token) return;
    if (consumedTokenRef.current === token) return;
    consumedTokenRef.current = token;
    (async () => {
      try {
        await api.devLogin(token);
        await refresh();
        nav("/", { replace: true });
      } catch (e) {
        setError(`Вход через dev-токен не удался: ${(e as Error).message}`);
      }
    })();
  }, [capturedToken, nav, refresh]);

  useEffect(() => {
    if (!BOT_USERNAME) return;
    // Не грузим внешний telegram-widget, пока в процессе вход по token/magic:
    // лишний внешний скрипт при активном секрет-флоу не нужен. (К моменту
    // этого effect'а секреты уже сняты с URL в consumeAuthSecretsFromUrl.)
    if (capturedToken || capturedMagic) return;

    window.onTelegramAuth = async (tgUser) => {
      try {
        await api.telegramLogin(tgUser);
        await refresh();
        nav("/", { replace: true });
      } catch (e) {
        setError(`Не удалось войти: ${(e as Error).message}`);
      }
    };

    const node = widgetRef.current;
    if (!node) return;

    const s = document.createElement("script");
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.async = true;
    s.setAttribute("data-telegram-login", BOT_USERNAME);
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "8");
    s.setAttribute("data-onauth", "onTelegramAuth(user)");
    s.setAttribute("data-request-access", "write");
    node.appendChild(s);

    return () => {
      node.replaceChildren();
    };
  }, [nav, refresh, capturedToken, capturedMagic]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--bg-canvas)] px-6">
      <div className="w-full max-w-md animate-riseIn text-center">
        <div className="mx-auto mb-8 flex h-20 w-20 animate-iconPop items-center justify-center rounded-full bg-[var(--bg-hover)] text-[var(--fg-primary)]">
          <SparklesIcon size={40} />
        </div>
        <h1 className="text-3xl font-semibold text-[var(--fg-primary)]">
          Vels-Claude
        </h1>
        <p className="mt-2 text-base text-[var(--fg-muted)]">
          Войдите через Telegram, чтобы продолжить
        </p>

        {/* Вход по логину/паролю (локальные аккаунты от админа) */}
        <form onSubmit={submitLogin} className="mt-10 space-y-3 text-left">
          <input
            type="text"
            autoComplete="username"
            placeholder="Логин"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full rounded-xl bg-[var(--bg-input)] px-4 py-3 text-[15px] text-[var(--fg-primary)] placeholder:text-[var(--fg-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--border-subtle)]"
          />
          <div className="relative">
            <input
              type={showPw ? "text" : "password"}
              autoComplete="current-password"
              placeholder="Пароль"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-xl bg-[var(--bg-input)] px-4 py-3 pr-20 text-[15px] text-[var(--fg-primary)] placeholder:text-[var(--fg-muted)] focus:outline-none focus:ring-2 focus:ring-[var(--border-subtle)]"
            />
            <button
              type="button"
              onClick={() => setShowPw((v) => !v)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-sm text-[var(--fg-muted)] hover:text-[var(--fg-primary)]"
            >
              {showPw ? "скрыть" : "показать"}
            </button>
          </div>
          <button
            type="submit"
            disabled={submitting || !username.trim() || !password}
            className="w-full rounded-xl bg-[var(--accent)] px-4 py-3 text-[15px] font-medium text-[var(--bg-canvas)] hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "Вход…" : "Войти"}
          </button>
        </form>

        <div className="mt-8">
          {BOT_USERNAME ? (
            <>
              <p className="mb-3 text-sm text-[var(--fg-muted)]">или через Telegram</p>
              <div ref={widgetRef} className="flex justify-center" />
            </>
          ) : null}
        </div>

        <p className="mt-8 text-xs text-[var(--fg-muted)]">
          Получить ссылку для входа: отправьте{" "}
          <code className="rounded-md bg-[var(--bg-hover)] px-1.5 py-0.5 text-[var(--fg-secondary)]">
            /weblogin
          </code>{" "}
          боту в Telegram.
        </p>

        {error && (
          <p className="mt-5 rounded-xl border border-red-700/40 bg-red-900/20 px-4 py-3 text-sm text-red-300">
            {error}
          </p>
        )}
      </div>
    </div>
  );
}
