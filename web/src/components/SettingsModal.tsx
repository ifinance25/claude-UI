import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { api } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import {
  CloseIcon,
  LogOutIcon,
  RefreshIcon,
  SettingsIcon,
  SparklesIcon,
} from "@/components/icons";
import {
  applyTheme,
  getStoredTheme,
  setStoredTheme,
  type Theme,
} from "@/lib/theme";
import { useModelInfo } from "@/lib/useModelInfo";
import ConnectionsPanel from "@/components/ConnectionsPanel";
import ApiKeyPanel from "@/components/ApiKeyPanel";

interface Props {
  open: boolean;
  onClose: () => void;
}

type Tab =
  | "general"
  | "model"
  | "verbose"
  | "connections"
  | "apikey"
  | "session";

const VERBOSE_LEVELS: { level: 0 | 1 | 2 | 3; label: string; hint: string }[] = [
  { level: 0, label: "Тихий", hint: "Только финальный ответ Claude" },
  { level: 1, label: "Нормальный", hint: "Ответ + вызовы инструментов" },
  { level: 2, label: "Подробный", hint: "+ размышления и логи субагентов" },
  { level: 3, label: "Детальный", hint: "Всё, включая внутренние события" },
];

const TABS: { id: Tab; label: string }[] = [
  { id: "general", label: "Общие" },
  { id: "model", label: "Модель" },
  { id: "verbose", label: "Логи" },
  { id: "connections", label: "Подключения" },
  { id: "apikey", label: "API-ключ" },
  { id: "session", label: "Аккаунт" },
];

export default function SettingsModal({ open, onClose }: Props) {
  const { user, logout } = useAuth();
  const [tab, setTab] = useState<Tab>("general");
  const [theme, setTheme] = useState<Theme>(getStoredTheme());
  const [verbose, setVerbose] = useState<0 | 1 | 2 | 3 | null>(null);
  const { info: model, error: modelError, setModel, refetch: refetchModel } = useModelInfo(false);
  const [saving, setSaving] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  // Когда последний раз перечитывали модель — внутри cache-window не
  // дёргаем бэк повторно (юзер часто открывает/закрывает модалку,
  // settings.json вряд ли поменялся за секунды).
  const lastModelRefetchRef = useRef(0);
  const MODEL_CACHE_WINDOW_MS = 30_000;

  // verbose грузится отдельно (не имеет общего хука), модель — через
  // useModelInfo. Cleanup-флаг ловит закрытие модала до завершения
  // промиса, чтобы setState не прилетал на размонтированный компонент.
  // Перечитываем модель при открытии только если кэш протух (>30 с) —
  // иначе доверяем pub-sub из useModelInfo: если кто-то менял модель
  // через InputModelButton, state уже свежий.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setSettingsError(null);
    const now = Date.now();
    if (now - lastModelRefetchRef.current > MODEL_CACHE_WINDOW_MS) {
      lastModelRefetchRef.current = now;
      void refetchModel();
    }
    if (verbose === null) {
      api
        .getSettings()
        .then((s) => {
          if (!cancelled) setVerbose(s.verbose_level);
        })
        .catch((e) => {
          if (!cancelled)
            setSettingsError(
              `Не удалось загрузить настройки: ${(e as Error).message}`,
            );
        });
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const error =
    settingsError ??
    (modelError ? `Не удалось загрузить модель: ${modelError}` : null);

  // Esc закрывает модал.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const onThemeChange = (next: Theme) => {
    setTheme(next);
    setStoredTheme(next);
    applyTheme(next);
  };

  const onVerboseChange = async (next: 0 | 1 | 2 | 3) => {
    setVerbose(next);
    setSaving(true);
    setSettingsError(null);
    try {
      await api.patchSettings(next);
    } catch (e) {
      setSettingsError(`Не удалось сохранить: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  const onModelChange = async (id: string) => {
    setSaving(true);
    setSettingsError(null);
    try {
      await setModel(id);
    } catch (e) {
      setSettingsError(`Не удалось сменить модель: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={onClose}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
        >
          <motion.div
            className="flex h-[640px] max-h-[90vh] w-full max-w-3xl overflow-hidden rounded-3xl bg-[var(--bg-elevated)] shadow-2xl ring-1 ring-[var(--border-subtle)]"
            onClick={(e) => e.stopPropagation()}
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.96 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
          >
        {/* Левая колонка — вкладки */}
        <aside className="flex w-56 shrink-0 flex-col border-r border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-3">
          <div className="flex items-center gap-2 px-3 py-3 text-base font-semibold text-[var(--fg-primary)]">
            <SettingsIcon size={20} />
            <span>Настройки</span>
          </div>
          <nav className="mt-2 space-y-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`w-full rounded-xl px-3 py-2 text-left text-sm transition-colors ${
                  tab === t.id
                    ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                    : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </aside>

        {/* Правая колонка — контент */}
        <section className="flex flex-1 flex-col">
          <header className="flex items-center justify-between px-6 py-4">
            <h2 className="text-lg font-semibold text-[var(--fg-primary)]">
              {TABS.find((t) => t.id === tab)?.label}
            </h2>
            <button
              onClick={onClose}
              className="rounded-xl p-2 text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
              title="Закрыть"
            >
              <CloseIcon size={18} />
            </button>
          </header>

          <div className="flex-1 overflow-y-auto px-6 pb-6">
            {error && (
              <div className="mb-4 rounded-xl border border-red-700/40 bg-red-900/20 px-3 py-2 text-sm text-red-300">
                {error}
              </div>
            )}

            {tab === "general" && (
              <div className="space-y-6">
                <div>
                  <div className="mb-3 text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
                    Тема
                  </div>
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
                        className={`rounded-xl px-5 py-2.5 text-sm transition-colors ${
                          theme === value
                            ? "bg-[var(--bg-hover)] text-[var(--fg-primary)] ring-1 ring-[var(--border-subtle)]"
                            : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {tab === "model" && (
              <div className="space-y-3">
                <div className="mb-2 flex items-center gap-2 text-[var(--fg-secondary)]">
                  <SparklesIcon size={18} />
                  <span className="text-sm">
                    Применится со следующего сообщения
                  </span>
                </div>
                {model === null ? (
                  <div className="text-sm text-[var(--fg-muted)]">Загрузка…</div>
                ) : (
                  model.known.map((m) => {
                    const active = m.id === model.current;
                    return (
                      <button
                        key={m.id}
                        onClick={() => void onModelChange(m.id)}
                        disabled={saving || active}
                        className={`flex w-full items-start gap-3 rounded-2xl border border-[var(--border-subtle)] p-4 text-left transition-colors ${
                          active
                            ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                            : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)] disabled:cursor-not-allowed"
                        }`}
                      >
                        <span
                          className={`mt-1.5 inline-block h-2.5 w-2.5 rounded-full ${
                            active ? "bg-emerald-500" : "bg-[var(--bg-hover)]"
                          }`}
                        />
                        <span className="flex-1">
                          <span className="block text-sm font-semibold">
                            {m.label}
                          </span>
                          <span className="mt-1 block text-xs text-[var(--fg-muted)]">
                            {m.hint}
                          </span>
                        </span>
                      </button>
                    );
                  })
                )}
                {model && (
                  <div className="pt-2 text-[11px] text-[var(--fg-muted)]">
                    Режим разрешений Claude: <code>{model.permission_mode}</code>
                  </div>
                )}
              </div>
            )}

            {tab === "verbose" && (
              <div className="space-y-2.5">
                <p className="text-sm text-[var(--fg-muted)]">
                  Сколько деталей выводить во время работы Claude. В браузере
                  лента вызовов инструментов видна всегда, а от уровня зависит
                  показ размышлений — со второго. В Telegram уровень управляет
                  всем выводом; там же он меняется командой <code>/verbose</code>.
                </p>
                {verbose === null ? (
                  <div className="text-sm text-[var(--fg-muted)]">Загрузка…</div>
                ) : (
                  VERBOSE_LEVELS.map(({ level, label, hint }) => (
                    <label
                      key={level}
                      className={`flex cursor-pointer items-start gap-4 rounded-2xl border border-[var(--border-subtle)] p-4 text-sm transition-colors ${
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
                          <span className="ml-2 text-xs text-[var(--fg-muted)]">
                            ({level})
                          </span>
                        </div>
                        <div className="mt-1 text-xs text-[var(--fg-muted)]">
                          {hint}
                        </div>
                      </div>
                      {saving && verbose === level && (
                        <RefreshIcon
                          size={16}
                          className="ml-auto animate-spin text-[var(--fg-muted)]"
                        />
                      )}
                    </label>
                  ))
                )}
              </div>
            )}

            {tab === "connections" && <ConnectionsPanel />}

            {tab === "apikey" && (
              <ApiKeyPanel privileged={user?.is_admin || false} />
            )}

            {tab === "session" && (
              <div className="space-y-6">
                <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-4 text-sm">
                  <div className="flex items-center justify-between py-1.5">
                    <span className="text-[var(--fg-muted)]">Telegram ID</span>
                    <span className="font-mono text-[var(--fg-primary)]">
                      {user?.id ?? "?"}
                    </span>
                  </div>
                  <div className="flex items-center justify-between py-1.5">
                    <span className="text-[var(--fg-muted)]">
                      Имя пользователя
                    </span>
                    <span className="text-[var(--fg-primary)]">
                      {user?.username || "—"}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => void logout()}
                  className="inline-flex items-center gap-2.5 rounded-xl bg-[var(--bg-hover)] px-4 py-2.5 text-sm text-[var(--fg-primary)] hover:opacity-80"
                >
                  <LogOutIcon size={18} />
                  <span>Выйти</span>
                </button>
              </div>
            )}
          </div>
        </section>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
