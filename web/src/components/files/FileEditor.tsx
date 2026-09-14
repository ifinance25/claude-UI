import { useState } from "react";
import { api, ApiError } from "@/api/client";
import type { FileContent } from "@/lib/types";

export default function FileEditor({
  projectPath,
  file,
  onSaved,
  onReload,
}: {
  projectPath: string;
  file: FileContent;
  onSaved: (mtimeNs: number, content: string) => void;
  onReload: () => void;
}) {
  const [text, setText] = useState(file.content);
  const [mtime, setMtime] = useState(file.mtime_ns);
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [errorMsg, setErrorMsg] = useState("");
  const [conflict, setConflict] = useState(false);
  const dirty = text !== file.content;

  // Понятная причина вместо статичного «ошибка» (как в остальных панелях):
  // readonly-юзер (или гонка смены прав) получает с бэка 403 — покажем, что
  // именно «только чтение», а не загадочную ошибку.
  function errorText(e: unknown): string {
    if (e instanceof ApiError) {
      if (e.status === 403) return "Только чтение — изменение запрещено";
      if (e.status === 413) return "Файл слишком большой";
    }
    return (e as Error).message || "ошибка";
  }

  async function save(expected: number) {
    setStatus("saving");
    setConflict(false);
    try {
      const r = await api.saveFile(projectPath, file.rel, text, expected);
      setMtime(r.mtime_ns);
      setStatus("saved");
      onSaved(r.mtime_ns, text);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setConflict(true);
        setStatus("idle");
      } else {
        setErrorMsg(errorText(e));
        setStatus("error");
      }
    }
  }

  async function overwrite() {
    // «Перезаписать»: берём актуальный mtime с диска и пишем поверх.
    try {
      const fresh = await api.fileContent(projectPath, file.rel);
      await save(fresh.mtime_ns);
    } catch (e) {
      setErrorMsg(errorText(e));
      setStatus("error");
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {conflict && (
        <div className="flex flex-wrap items-center gap-3 bg-amber-900/30 px-3 py-2 text-sm text-amber-300">
          <span>Файл изменился на диске.</span>
          <button onClick={onReload} className="underline">Перезагрузить</button>
          <button onClick={overwrite} className="underline">Перезаписать</button>
        </div>
      )}
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        className="min-h-0 flex-1 resize-none bg-transparent p-3 font-mono text-sm text-[var(--fg-primary)] focus:outline-none"
      />
      <div className="flex items-center gap-3 border-t border-[var(--border-subtle)] px-3 py-2">
        <button
          disabled={!dirty || status === "saving"}
          onClick={() => save(mtime)}
          className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg-canvas)] disabled:opacity-50"
        >
          Сохранить
        </button>
        {status === "saving" && <span className="text-[var(--fg-muted)]">сохраняем…</span>}
        {status === "saved" && <span className="text-emerald-500">сохранено</span>}
        {status === "error" && (
          <span className="text-red-400">{errorMsg || "ошибка"}</span>
        )}
      </div>
    </div>
  );
}
