import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { api } from "@/api/client";
import { CloseIcon, FileIcon, FolderIcon, MaximizeIcon, RefreshIcon } from "@/components/icons";
import ResizeHandle from "@/components/ResizeHandle";
import { usePanelWidth } from "@/lib/usePanelWidth";
import type { Artifact } from "@/lib/types";

function formatSize(n: number | null): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function ArtifactRow({
  a,
  sessionUuid,
  canDelete,
  onHide,
  onDelete,
}: {
  a: Artifact;
  sessionUuid: string;
  canDelete: boolean;
  onHide: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="flex items-center justify-between gap-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]/60 px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        {a.is_dir ? (
          <FolderIcon size={16} className="shrink-0 text-[var(--fg-muted)]" />
        ) : (
          <FileIcon size={16} className="shrink-0 text-[var(--fg-muted)]" />
        )}
        <div className="min-w-0">
          <div
            className="truncate text-sm text-[var(--fg-primary)]"
            title={a.display}
          >
            {a.name}
            {a.is_dir && <span className="text-[var(--fg-muted)]"> /</span>}
          </div>
          <div className="flex items-center gap-1.5 text-xs text-[var(--fg-muted)]">
            <span>{a.action === "created" ? "создан" : "изменён"}</span>
            {a.edits > 1 && <span>· ×{a.edits}</span>}
            {a.exists ? (
              a.size_bytes != null && <span>· {formatSize(a.size_bytes)}</span>
            ) : (
              <span className="text-amber-500">· удалён</span>
            )}
          </div>
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5 text-xs">
        {a.exists && !confirming && (
          <a
            href={api.sessionFileUrl(sessionUuid, a.rel)}
            download={a.is_dir ? `${a.name}.zip` : a.name}
            className="rounded-lg bg-[var(--accent)] px-2.5 py-1 font-medium text-[var(--bg-canvas)] hover:opacity-90"
            title={a.is_dir ? "Скачать папку (zip)" : "Скачать файл"}
          >
            {a.is_dir ? "Скачать zip" : "Скачать"}
          </a>
        )}
        {!confirming ? (
          <>
            <button
              onClick={onHide}
              className="rounded-lg px-2 py-1 text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
              title="Скрыть из списка"
            >
              Скрыть
            </button>
            {canDelete && a.exists && (
              <button
                onClick={() => setConfirming(true)}
                className="rounded-lg px-2 py-1 text-red-400 hover:text-red-300"
                title="Удалить файл с диска"
              >
                Удалить
              </button>
            )}
          </>
        ) : (
          <>
            <span className="text-[var(--fg-muted)]">Удалить файл?</span>
            <button
              onClick={() => {
                setConfirming(false);
                onDelete();
              }}
              className="rounded-lg bg-red-500/80 px-2 py-1 font-medium text-white hover:bg-red-500"
            >
              Да
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="rounded-lg px-2 py-1 text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
            >
              Нет
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default function ArtifactsPanel({
  projectPath,
  sessionUuid,
  onClose,
}: {
  projectPath: string;
  sessionUuid: string;
  onClose: () => void;
}) {
  const [accessLevel, setAccessLevel] = useState<"full" | "readonly">("readonly");
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.filesArtifacts(projectPath).then(
      (r) => {
        if (cancelled) return;
        setAccessLevel(r.access_level);
        setArtifacts(r.artifacts);
        setLoading(false);
      },
      (e) => {
        if (cancelled) return;
        setError((e as Error).message);
        setLoading(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [projectPath, reloadToken]);

  async function hide(rel: string) {
    setError(null);
    try {
      await api.dismissArtifact(projectPath, rel);
      setArtifacts((prev) =>
        prev.map((a) => (a.rel === rel ? { ...a, dismissed: true } : a)),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function unhide(rel: string) {
    setError(null);
    try {
      await api.undismissArtifact(projectPath, rel);
      setArtifacts((prev) =>
        prev.map((a) => (a.rel === rel ? { ...a, dismissed: false } : a)),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function remove(rel: string) {
    setError(null);
    try {
      await api.deleteEntry(projectPath, rel);
      setArtifacts((prev) =>
        prev.map((a) => (a.rel === rel ? { ...a, exists: false } : a)),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const visible = artifacts.filter((a) => (showHidden ? a.dismissed : !a.dismissed));
  const hiddenCount = artifacts.filter((a) => a.dismissed).length;
  const { width, startResize, toggleMax } = usePanelWidth();

  return (
    <aside
      className="fixed inset-0 z-30 flex w-full animate-fadeInUp flex-col border-l border-[var(--border-subtle)] bg-[var(--bg-sidebar)] md:relative md:z-auto md:w-[var(--panel-w)] md:shrink-0"
      style={{ "--panel-w": `${width}px` } as CSSProperties}
    >
      <ResizeHandle onPointerDown={startResize} />
      <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-3 py-2">
        <span className="text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
          Артефакты{!loading && accessLevel === "readonly" && " · только чтение"}
        </span>
        <div className="flex items-center gap-1 text-[var(--fg-secondary)]">
          <button
            onClick={() => setReloadToken((t) => t + 1)}
            className="icon-btn rounded p-1 hover:text-[var(--fg-primary)]"
            title="Обновить"
            aria-label="Обновить"
          >
            <RefreshIcon size={16} />
          </button>
          <button
            onClick={toggleMax}
            className="icon-btn hidden rounded p-1 hover:text-[var(--fg-primary)] md:inline-flex"
            title="Развернуть / свернуть панель"
            aria-label="Развернуть панель"
          >
            <MaximizeIcon size={16} />
          </button>
          <button
            onClick={onClose}
            className="icon-btn rounded p-1 hover:text-[var(--fg-primary)]"
            title="Закрыть"
            aria-label="Закрыть"
          >
            <CloseIcon size={16} />
          </button>
        </div>
      </div>

      {error && <div className="px-3 py-2 text-sm text-red-400">{error}</div>}

      {(showHidden || hiddenCount > 0) && (
        <button
          onClick={() => setShowHidden((v) => !v)}
          className="border-b border-[var(--border-subtle)] px-3 py-1.5 text-left text-xs text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
        >
          {showHidden ? "← К списку" : `Показать скрытые (${hiddenCount})`}
        </button>
      )}

      <div className="min-h-0 flex-1 overflow-auto p-2">
        {loading ? (
          <div className="px-1 py-2 text-sm text-[var(--fg-muted)]">Загрузка…</div>
        ) : visible.length === 0 ? (
          <div className="px-1 py-2 text-sm text-[var(--fg-muted)]">
            {showHidden
              ? "Нет скрытых артефактов."
              : "Claude ещё не создавал файлов в этом проекте."}
          </div>
        ) : (
          <div className="space-y-2">
            {visible.map((a) =>
              showHidden ? (
                <div
                  key={a.rel}
                  className="flex items-center justify-between gap-2 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]/30 px-3 py-2 opacity-70"
                >
                  <div className="min-w-0">
                    <div
                      className="truncate text-sm text-[var(--fg-primary)]"
                      title={a.display}
                    >
                      {a.name}
                    </div>
                    <div className="text-xs text-[var(--fg-muted)]">скрыт</div>
                  </div>
                  <button
                    onClick={() => unhide(a.rel)}
                    className="shrink-0 rounded-lg px-2.5 py-1 text-xs text-[var(--accent)] hover:opacity-90"
                  >
                    Вернуть
                  </button>
                </div>
              ) : (
                <ArtifactRow
                  key={a.rel}
                  a={a}
                  sessionUuid={sessionUuid}
                  canDelete={accessLevel === "full"}
                  onHide={() => hide(a.rel)}
                  onDelete={() => remove(a.rel)}
                />
              ),
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
