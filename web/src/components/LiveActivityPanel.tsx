/**
 * Раскрываемая панель живых «мыслей» Claude (поток thinking-дельт активного
 * запроса). Без печатной машинки и без тяжёлого Markdown — мысли идут быстро,
 * незакрытый markdown «прыгал» бы; выводим приглушённым pre-wrap текстом.
 */
import { LightbulbIcon } from "@/components/icons";

export function LiveActivityPanel({
  id,
  thinking,
}: {
  id: string;
  thinking: string;
}) {
  return (
    <div
      id={id}
      className="mb-2 overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]"
    >
      <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-4 py-2 text-[13px] font-medium text-[var(--fg-secondary)]">
        <LightbulbIcon size={15} className="shrink-0" />
        <span>Размышления</span>
      </div>
      <div className="max-h-[40vh] overflow-y-auto px-5 py-3 text-[13px] leading-relaxed text-[var(--fg-muted)]">
        {thinking.trim() ? (
          <p className="whitespace-pre-wrap break-words">{thinking}</p>
        ) : (
          <span className="italic">Подключаюсь…</span>
        )}
      </div>
    </div>
  );
}
