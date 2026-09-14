import { useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronDownIcon, providerLogo } from "@/components/icons";
import { useClickOutside } from "@/lib/useClickOutside";
import { useModelInfo } from "@/lib/useModelInfo";

interface Props {
  /** Вызывается после успешной смены модели — чтобы родитель показал toast. */
  onChange?: (model: string) => void;
}

/**
 * Компактный селектор модели, встроенный слева в инпут (там, где
 * раньше был неработающий «+»). Dropdown открывается ВВЕРХ, чтобы
 * не уходить за нижний край окна.
 */
export default function InputModelButton({ onChange }: Props) {
  // useFallback=true — даже если бэк недоступен, показываем кнопку с
  // дефолтным списком моделей, а не пустую/мёртвую.
  const { info, error: loadError, setModel } = useModelInfo(true);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [patchError, setPatchError] = useState<string | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  useClickOutside(wrapRef, () => setOpen(false), open);

  const pick = async (id: string) => {
    if (!info) return;
    if (id === info.current) {
      setOpen(false);
      return;
    }
    setBusy(true);
    try {
      const updated = await setModel(id);
      onChange?.(updated.current);
      setPatchError(null);
      setOpen(false);
    } catch (e) {
      // 403 = смену глобальной модели разрешено только админу (require_admin).
      // Показываем понятную причину вместо технического «403 Forbidden: admin only».
      const status = (e as { status?: number }).status;
      setPatchError(
        status === 403
          ? "Менять модель может только администратор."
          : (e as Error).message,
      );
    } finally {
      setBusy(false);
    }
  };

  const current = info?.current ?? "claude-sonnet-4-6";
  const knownItem = info?.known.find((m) => m.id === current);
  // Короткая подпись для тесного места в инпуте: "Sonnet 4.6" вместо
  // полного "Claude Sonnet 4.6".
  const shortLabel = (knownItem?.label ?? current).replace(/^Claude\s+/, "");

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        className="flex items-center gap-2 rounded-full bg-[var(--bg-hover)] px-3 py-1.5 text-sm text-[var(--fg-primary)] hover:opacity-90 disabled:opacity-60"
        title="Сменить модель Claude"
      >
        {providerLogo(current, 16)}
        <span
          className="hidden max-w-[160px] truncate sm:inline"
          title={knownItem?.label ?? current}
        >
          {shortLabel}
        </span>
        <ChevronDownIcon size={14} />
      </button>
      <AnimatePresence>
        {open && info && (
        <motion.div
          initial={{ opacity: 0, y: 8, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 8, scale: 0.98 }}
          transition={{ duration: 0.16, ease: "easeOut" }}
          className="absolute bottom-full left-0 z-30 mb-2 w-80 origin-bottom overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] shadow-xl"
        >
          <div className="px-4 pt-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
            Модель Claude
          </div>
          {info.known.map((m) => {
            const active = m.id === info.current;
            return (
              <button
                key={m.id}
                onClick={() => void pick(m.id)}
                disabled={busy}
                className={`flex w-full items-start gap-3 px-4 py-3 text-left transition-colors ${
                  active
                    ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                    : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                }`}
              >
                <span
                  className={`mt-1.5 inline-block h-2 w-2 rounded-full ${
                    active ? "bg-emerald-500" : "bg-transparent"
                  }`}
                />
                <span className="mt-0.5">{providerLogo(m.id, 16)}</span>
                <span className="flex-1">
                  <span className="block text-sm font-semibold">{m.label}</span>
                  <span className="mt-0.5 block text-xs text-[var(--fg-muted)]">
                    {m.hint}
                  </span>
                </span>
              </button>
            );
          })}
          <div className="border-t border-[var(--border-subtle)] px-4 py-2 text-[11px] text-[var(--fg-muted)]">
            Применится со следующего сообщения · режим разрешений:{" "}
            <code>{info.permission_mode}</code>
            {patchError ? (
              // Ошибка СМЕНЫ (например 403 «только админ») — показываем как есть.
              <span className="mt-1 block text-amber-400">⚠ {patchError}</span>
            ) : loadError ? (
              // Ошибка ЗАГРУЗКИ — список дефолтный, но кнопка рабочая.
              <span className="mt-1 block text-amber-400">
                ⚠ Не удалось загрузить текущую модель — показаны значения по
                умолчанию. Если не сохраняется, обновите страницу или войдите
                заново.
              </span>
            ) : null}
          </div>
        </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
