import { useEffect, useState } from "react";
import { ChevronRightIcon, LightbulbIcon } from "@/components/icons";
import { Markdown } from "@/lib/Markdown";

/** Свёрнутый блок «🧠 Размышления» (виден на verbose ≥ 2). */
export function ThinkingEvent({
  blocks,
  defaultOpen = false,
}: {
  blocks: string[];
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  // Синхронизируем с defaultOpen: живой бабл рендерится свёрнутым
  // (defaultOpen=false), а по завершении ответа defaultOpen→true → раскрываем,
  // чтобы при verbose ≥ 2 рассуждения было видно как раньше.
  useEffect(() => {
    setOpen(defaultOpen);
  }, [defaultOpen]);
  if (blocks.length === 0) return null;
  return (
    <div
      className={`${
        open ? "my-2" : "my-1"
      } overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]/60`}
    >
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)]/50"
      >
        <ChevronRightIcon
          size={16}
          className={`shrink-0 transition-transform duration-200 ease-spring ${
            open ? "rotate-90" : ""
          }`}
        />
        <LightbulbIcon size={15} className="shrink-0" />
        <span className="text-[13px] font-medium">Размышления</span>
      </button>
      {open && (
        <div className="border-t border-[var(--border-subtle)] bg-[var(--bg-canvas)]/40 px-5 py-3 text-[var(--fg-secondary)]">
          {blocks.map((b, i) => (
            <Markdown key={i}>{b}</Markdown>
          ))}
        </div>
      )}
    </div>
  );
}
