import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { LogOutIcon, RefreshIcon } from "@/components/icons";
import {
  applyTheme,
  getStoredTheme,
  setStoredTheme,
  type Theme,
} from "@/lib/theme";

const VERBOSE_LEVELS: { level: 0 | 1 | 2 | 3; label: string; hint: string }[] = [
  { level: 0, label: "Тихий", hint: "Только финальный ответ Claude" },
  { level: 1, label: "Нормальный", hint: "Ответ + вызовы инструментов" },
  { level: 2, label: "Подробный", hint: "+ логи субагентов" },
  { level: 3, label: "Детальный", hint: "Всё, включая внутренние события" },
];

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h2 className="mb-4 text-[13px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
        {title}
      </h2>
      {children}
    </section>
  );
}

export default function SettingsPage() {
  const { user, logout } = useAuth();
  const [theme, setTheme] = useState<Theme>(getStoredTheme());
  const [verbose, setVerbose] = useState<0 | 1 | 2 | 3 | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const s = await api.getSettings();
        setVerbose(s.verbose_level);
      } catch (e) {
        setError(`Не удалось загрузить настройки: ${(e as Error).message}`);
      }
    })();
  }, []);

  const onThemeChange = (next: Theme) => {
    setTheme(next);
    setStoredTheme(next);
    applyTheme(next);
  };

  const onVerboseChange = async (next: 0 | 1 | 2 | 3) => {
    setVerbose(next);
    setSaving(true);
    setError(null);
    try {
      await api.patchSettings(next);
    } catch (e) {
      setError(`Не удалось сохранить: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="min-h-screen bg-[var(--bg-canvas)]">
      <div className="mx-auto flex h-screen max-w-3xl flex-col">
        <header className="flex items-center justify-between px-8 py-6">
          <h1 className="text-xl font-semibold text-[var(--fg-primary)]">
            Настройки
          </h1>
          <Link
            to="/"
            className="rounded-xl px-4 py-2 text-sm text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
          >
            ← Назад
          </Link>
        </header>

        <div className="flex-1 space-y-10 overflow-y-auto px-8 pb-12">
          <Section title="Профиль">
            <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-5 text-[15px]">
              <div className="flex items-center justify-between py-2">
                <span className="text-[var(--fg-muted)]">Telegram ID</span>
                <span className="font-mono text-[var(--fg-primary)]">
                  {user?.id ?? "?"}
                </span>
              </div>
              <div className="flex items-center justify-between py-2">
                <span className="text-[var(--fg-muted)]">Имя пользователя</span>
                <span className="text-[var(--fg-primary)]">
                  {user?.username || "—"}
                </span>
              </div>
            </div>
          </Section>

          <Section title="Тема">
            <div className="flex gap-2">
              {(
                [
                  { value: "dark" as Theme, label: "Тёмная" },
                  { value: "light" as Theme, label: "Светлая" },
                ]
              ).map(({ value, label }) => (
                <button
                  key={value}
                  onClick={() => onThemeChange(value)}
                  className={`rounded-xl px-5 py-2.5 text-[15px] transition-colors ${
                    theme === value
                      ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                      : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </Section>

          <Section title="Подробность вывода">
            {verbose === null ? (
              <div className="text-sm text-[var(--fg-muted)]">Загрузка…</div>
            ) : (
              <div className="space-y-2.5">
                {VERBOSE_LEVELS.map(({ level, label, hint }) => (
                  <label
                    key={level}
                    className={`flex cursor-pointer items-start gap-4 rounded-2xl border border-[var(--border-subtle)] p-4 text-[15px] transition-colors ${
                      verbose === level
                        ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                        : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                    }`}
                  >
                    <input
                      type="radio"
                      name="verbose"
                      className="mt-1.5 h-4 w-4 accent-white"
                      checked={verbose === level}
                      onChange={() => void onVerboseChange(level)}
                      disabled={saving}
                    />
                    <div>
                      <div className="font-semibold">
                        {label}
                        <span className="ml-2 text-sm text-[var(--fg-muted)]">
                          ({level})
                        </span>
                      </div>
                      <div className="mt-1 text-sm text-[var(--fg-muted)]">
                        {hint}
                      </div>
                    </div>
                    {saving && verbose === level && (
                      <RefreshIcon
                        size={18}
                        className="ml-auto animate-spin text-[var(--fg-muted)]"
                      />
                    )}
                  </label>
                ))}
              </div>
            )}
          </Section>

          <Section title="Сессия">
            <button
              onClick={() => void logout()}
              className="inline-flex items-center gap-2.5 rounded-xl bg-[var(--bg-hover)] px-4 py-2.5 text-[15px] text-[var(--fg-primary)] hover:opacity-80"
            >
              <LogOutIcon size={18} />
              <span>Выйти</span>
            </button>
          </Section>

          {error && (
            <div className="rounded-2xl border border-red-700/40 bg-red-900/20 px-4 py-3 text-sm text-red-300">
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
