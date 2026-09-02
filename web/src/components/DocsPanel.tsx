import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { CloseIcon } from "@/components/icons";
import { Markdown } from "@/lib/Markdown";

export default function DocsPanel({ onClose }: { onClose: () => void }) {
  const [content, setContent] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await api.getGuide();
        if (!cancelled) setContent(d.content);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mx-auto w-full max-w-4xl px-6 pt-4">
      <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-4">
        <div className="mb-2 flex items-center justify-between text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
          <span>Документация</span>
          <button onClick={onClose} className="rounded p-1 hover:text-[var(--fg-primary)]">
            <CloseIcon size={16} />
          </button>
        </div>
        {error && <div className="text-sm text-red-400">{error}</div>}
        {/* Фиксированная высота — чтобы при асинхронной подгрузке гайда
            высота панели НЕ прыгала после анимации открытия (иначе контент
            «резко появляется»). Контент скроллится внутри. */}
        <div className="h-[60vh] overflow-y-auto pr-2">
          {content ? (
            <Markdown>{content}</Markdown>
          ) : (
            !error && (
              <div className="flex h-full items-center justify-center text-sm text-[var(--fg-muted)]">
                Загрузка…
              </div>
            )
          )}
        </div>
      </div>
    </div>
  );
}
