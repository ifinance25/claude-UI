import { useEffect, useRef, useState } from "react";
import { contextZone, type CtxZone } from "@/lib/contextGauge";

interface Props {
  /** Заполнение окна в %, 0–100. */
  pct: number;
  /** Токены контекста последнего хода (input + cache read + cache create). */
  contextTokens: number;
  /** Размер окна модели (сейчас 1000000). */
  window: number;
  onNewChat: () => void;
  onCompact: () => void;
  /** Счётчик: растёт при каждом пересечении 85% → поповер авто-раскрывается один раз. */
  autoOpenTrigger: number;
}

const ZONE_COLOR: Record<CtxZone, string> = {
  green: "#3fb950",
  amber: "#d9a15c",
  red: "#e5534b",
};

function humanTokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

export default function ContextGauge({
  pct,
  contextTokens,
  window,
  onNewChat,
  onCompact,
  autoOpenTrigger,
}: Props) {
  const [open, setOpen] = useState(false);
  const seenTrigger = useRef(autoOpenTrigger);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoOpenTrigger > seenTrigger.current) {
      seenTrigger.current = autoOpenTrigger;
      setOpen(true);
    }
  }, [autoOpenTrigger]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (contextTokens <= 0) return null;

  const zone = contextZone(pct);
  const color = ZONE_COLOR[zone];
  const remaining = Math.max(0, window - contextTokens);

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        data-zone={zone}
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-hover)]/60 px-2.5 py-1 text-xs tabular-nums text-[var(--fg-secondary)] transition-colors hover:text-[var(--fg-primary)]"
        title="Заполнение контекстного окна"
        aria-label={`Контекст ${pct}%`}
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        <span
          className="inline-flex h-[18px] w-[18px] items-center justify-center rounded-full"
          style={{ background: `conic-gradient(${color} ${pct}%, var(--bg-canvas) 0)` }}
        >
          <span className="h-[11px] w-[11px] rounded-full bg-[var(--bg-input)]" />
        </span>
        <span>{pct}% контекст</span>
      </button>

      {open && (
        <div
          role="dialog"
          className="absolute bottom-full left-0 z-20 mb-2 w-64 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3 text-xs text-[var(--fg-secondary)] shadow-lg"
        >
          <div className="mb-1 font-semibold text-[var(--fg-primary)]">Контекст {pct}%</div>
          <div className="tabular-nums">
            {contextTokens.toLocaleString()} / {window.toLocaleString()} токенов
          </div>
          <div className="mt-0.5 text-[var(--fg-muted)]">до заполнения ~{humanTokens(remaining)}</div>
          {zone === "red" && (
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={() => { setOpen(false); onNewChat(); }}
                className="rounded-lg bg-[var(--accent)] px-3 py-1.5 font-medium text-[var(--bg-canvas)] hover:opacity-90"
              >
                Новый чат
              </button>
              <button
                type="button"
                onClick={() => { setOpen(false); onCompact(); }}
                className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-hover)] px-3 py-1.5 text-[var(--fg-primary)] hover:bg-[var(--bg-canvas)]"
              >
                Сжать
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
