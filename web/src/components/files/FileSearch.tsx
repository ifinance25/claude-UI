// web/src/components/files/FileSearch.tsx
import { useState } from "react";
import { api } from "@/api/client";
import type { SearchHit } from "@/lib/types";

export default function FileSearch({
  projectPath,
  onOpen,
}: {
  projectPath: string;
  onOpen: (rel: string, line: number | null) => void;
}) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<"name" | "content">("name");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  async function run() {
    const query = q.trim();
    if (!query) {
      setHits([]);
      setTruncated(false);
      return;
    }
    setBusy(true);
    setError(false);
    try {
      const r = await api.searchFiles(projectPath, query, mode);
      setHits(r.hits);
      setTruncated(r.truncated);
    } catch {
      setError(true);
      setHits([]);
      setTruncated(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 p-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="Поиск…"
          className="min-w-0 flex-1 rounded-md bg-[var(--bg-input)] px-2 py-1 text-sm text-[var(--fg-primary)]"
        />
        <button
          onClick={() => setMode((m) => (m === "name" ? "content" : "name"))}
          className="rounded-md px-2 py-1 text-xs text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)]"
          title="Переключить режим поиска"
        >
          {mode === "name" ? "имя" : "текст"}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-2 text-sm">
        {busy && <div className="px-2 py-1 text-[var(--fg-muted)]">…</div>}
        {!busy && error && <div className="px-2 py-1 text-red-400">Ошибка поиска</div>}
        {!busy && !error && hits.length === 0 && q.trim() && (
          <div className="px-2 py-1 text-[var(--fg-muted)]">Ничего не найдено</div>
        )}
        {hits.map((h, i) => (
          <button
            key={`${h.rel}:${h.line ?? 0}:${i}`}
            onClick={() => onOpen(h.rel, h.line)}
            className="block w-full truncate rounded px-2 py-1 text-left hover:bg-[var(--bg-hover)]"
          >
            <span className="text-[var(--fg-primary)]">{h.rel}</span>
            {h.line != null && (
              <span className="ml-2 text-[var(--fg-muted)]">
                :{h.line} {h.preview ?? ""}
              </span>
            )}
          </button>
        ))}
        {truncated && (
          <div className="px-2 py-1 text-xs italic text-[var(--fg-muted)]">
            … показаны не все совпадения (слишком много)
          </div>
        )}
      </div>
    </div>
  );
}
