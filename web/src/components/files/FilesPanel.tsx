import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { CloseIcon, MaximizeIcon, RefreshIcon, SearchIcon } from "@/components/icons";
import FileTree from "@/components/files/FileTree";
import FileViewer from "@/components/files/FileViewer";
import FileEditor from "@/components/files/FileEditor";
import FileSearch from "@/components/files/FileSearch";
import ResizeHandle from "@/components/ResizeHandle";
import { usePanelWidth } from "@/lib/usePanelWidth";
import type { CSSProperties } from "react";
import type { FileContent } from "@/lib/types";

export default function FilesPanel({
  projectPath,
  sessionUuid,
  onClose,
}: {
  projectPath: string;
  sessionUuid: string;
  onClose: () => void;
}) {
  const [accessLevel, setAccessLevel] = useState<"full" | "readonly">("readonly");
  // Пока уровень не загружен — не показываем ярлык «только чтение» (иначе у
  // full-юзера мигает неверная пометка на дефолтном "readonly").
  const [accessLoaded, setAccessLoaded] = useState(false);
  const [open, setOpen] = useState<FileContent | null>(null);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [searching, setSearching] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // Строка из результата поиска по содержимому — показываем её в шапке файла.
  const [openLine, setOpenLine] = useState<number | null>(null);

  // Уровень доступа берём из корневого /tree (он же первым грузит дерево).
  useEffect(() => {
    let cancelled = false;
    api.filesTree(projectPath, "").then(
      (r) => {
        if (!cancelled) {
          setAccessLevel(r.access_level);
          setAccessLoaded(true);
        }
      },
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, [projectPath, reloadToken]);

  async function openFile(rel: string, line: number | null = null) {
    setError(null);
    setSearching(false);
    try {
      const f = await api.fileContent(projectPath, rel);
      setOpen(f);
      setOpenLine(line);
      setMode("view");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  function download(rel: string) {
    window.open(api.sessionFileUrl(sessionUuid, rel), "_blank");
  }

  const canEdit = accessLevel === "full";
  const { width, startResize, toggleMax } = usePanelWidth();

  return (
    <aside
      className="fixed inset-0 z-30 flex w-full animate-fadeInUp flex-col border-l border-[var(--border-subtle)] bg-[var(--bg-sidebar)] md:relative md:z-auto md:w-[var(--panel-w)] md:shrink-0"
      style={{ "--panel-w": `${width}px` } as CSSProperties}
    >
      <ResizeHandle onPointerDown={startResize} />
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-3 py-2">
        <span className="text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
          Файлы{accessLoaded && accessLevel === "readonly" && " · только чтение"}
        </span>
        <div className="flex items-center gap-1 text-[var(--fg-secondary)]">
          <button onClick={() => setSearching((v) => !v)} className="icon-btn rounded p-1 hover:text-[var(--fg-primary)]" title="Поиск" aria-label="Поиск">
            <SearchIcon size={16} />
          </button>
          <button onClick={() => setReloadToken((t) => t + 1)} className="icon-btn rounded p-1 hover:text-[var(--fg-primary)]" title="Обновить" aria-label="Обновить">
            <RefreshIcon size={16} />
          </button>
          <button onClick={toggleMax} className="icon-btn hidden rounded p-1 hover:text-[var(--fg-primary)] md:inline-flex" title="Развернуть / свернуть панель" aria-label="Развернуть панель">
            <MaximizeIcon size={16} />
          </button>
          <button onClick={onClose} className="icon-btn rounded p-1 hover:text-[var(--fg-primary)]" title="Закрыть" aria-label="Закрыть">
            <CloseIcon size={16} />
          </button>
        </div>
      </div>

      {error && <div className="px-3 py-2 text-sm text-red-400">{error}</div>}

      <div className="flex min-h-0 flex-1 flex-col">
        {searching ? (
          <FileSearch projectPath={projectPath} onOpen={(rel, line) => openFile(rel, line)} />
        ) : open ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="flex items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-1.5 text-sm">
              <span className="min-w-0 truncate text-[var(--fg-primary)]">
                {open.rel}
                {openLine != null && (
                  <span className="text-[var(--fg-muted)]">:{openLine}</span>
                )}
              </span>
              <div className="flex shrink-0 items-center gap-2">
                {canEdit && !open.binary && !open.too_large && (
                  <button onClick={() => setMode((m) => (m === "edit" ? "view" : "edit"))} className="text-[var(--accent)]">
                    {mode === "edit" ? "Просмотр" : "Править"}
                  </button>
                )}
                <a
                  href={api.sessionFileUrl(sessionUuid, open.rel)}
                  download={open.rel.split("/").pop()}
                  className="text-[var(--accent)] hover:opacity-90"
                  title="Скачать файл"
                >
                  Скачать
                </a>
                <button onClick={() => setOpen(null)} className="icon-btn text-[var(--fg-muted)] hover:text-[var(--fg-primary)]" title="Закрыть файл" aria-label="Закрыть файл">
                  <CloseIcon size={16} />
                </button>
              </div>
            </div>
            {mode === "edit" && canEdit ? (
              <FileEditor
                projectPath={projectPath}
                file={open}
                onSaved={(mtimeNs, content) => setOpen({ ...open, mtime_ns: mtimeNs, content })}
                onReload={() => openFile(open.rel)}
              />
            ) : (
              <FileViewer
                file={open}
                inlineUrl={api.sessionFileInlineUrl(sessionUuid, open.rel)}
                downloadUrl={api.sessionFileUrl(sessionUuid, open.rel)}
                onDownload={() => download(open.rel)}
              />
            )}
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-auto py-1">
            <FileTree
              projectPath={projectPath}
              canEdit={canEdit}
              onOpenFile={(rel) => openFile(rel)}
              downloadHref={(rel) => api.sessionFileUrl(sessionUuid, rel)}
              reloadToken={reloadToken}
            />
          </div>
        )}
      </div>
    </aside>
  );
}
