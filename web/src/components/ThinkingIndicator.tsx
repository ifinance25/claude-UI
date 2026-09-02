/**
 * «Живой» индикатор активности Claude: меняющийся глагол (по фазе) + контекст
 * текущего действия + таймер. Кликабельная строка-тоггл раскрывает панель мыслей.
 */
import { useEffect, useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, ClaudeLogo } from "@/components/icons";
import { displayVerb, type LiveActivity } from "@/lib/liveActivity";
import { prefersReducedMotion } from "@/lib/reducedMotion";

function fmt(elapsedMs: number): string {
  const s = Math.floor(elapsedMs / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
}

export default function ThinkingIndicator({
  activity,
  elapsedMs,
  open,
  onToggle,
  panelId,
}: {
  activity: LiveActivity;
  elapsedMs?: number | null;
  open: boolean;
  onToggle: () => void;
  panelId: string;
}) {
  // Ротация глагола в фазе «thinking». Гейтим JS-интервал на reduced-motion —
  // CSS prefers-reduced-motion его НЕ остановит.
  const reduced = prefersReducedMotion();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (reduced || activity.phase !== "thinking") return;
    const id = window.setInterval(() => setTick((t) => t + 1), 3000);
    return () => window.clearInterval(id);
  }, [reduced, activity.phase]);

  const verb = displayVerb(activity, tick, reduced);

  return (
    <button
      onClick={onToggle}
      aria-expanded={open}
      aria-controls={panelId}
      aria-label="Claude работает — показать размышления"
      className="my-2 flex w-full items-center gap-3 rounded-xl px-2 py-1.5 text-left text-[var(--fg-muted)] transition-colors hover:bg-[var(--bg-hover)]/40"
    >
      <span className="shrink-0 animate-pulse" aria-hidden="true">
        <ClaudeLogo size={18} />
      </span>
      <span className="flex gap-1" aria-hidden="true">
        <span className="h-2 w-2 animate-bounce rounded-full bg-[var(--fg-muted)] [animation-delay:-0.32s]" />
        <span className="h-2 w-2 animate-bounce rounded-full bg-[var(--fg-muted)] [animation-delay:-0.16s]" />
        <span className="h-2 w-2 animate-bounce rounded-full bg-[var(--fg-muted)]" />
      </span>
      <span className="text-sm">
        {verb}…
        {activity.context && (
          <span className="ml-2 text-[var(--fg-secondary)]">{activity.context}</span>
        )}
      </span>
      {typeof elapsedMs === "number" && elapsedMs > 0 && (
        <span className="font-mono text-xs tabular-nums text-[var(--fg-muted)]">
          {fmt(elapsedMs)}
        </span>
      )}
      <span className="ml-auto shrink-0" aria-hidden="true">
        {open ? <ChevronDownIcon size={16} /> : <ChevronRightIcon size={16} />}
      </span>
    </button>
  );
}
