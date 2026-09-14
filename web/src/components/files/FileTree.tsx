// web/src/components/files/FileTree.tsx
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { FileEntry } from "@/lib/types";
import { CheckIcon, ChevronRightIcon, CloseIcon, DownloadIcon, EditIcon, FolderPlusIcon, PlusIcon, TrashIcon } from "@/components/icons";

type Inline = "new-file" | "new-dir" | "rename" | null;

// ── VS-Code-стиль иконки (без зависимостей, inline SVG) ─────────────
const FOLDER_COLOR = "#dcb67a";

// Цвет иконки файла по расширению (VS Code-ish палитра)
function fileIconColor(name: string): string {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  switch (ext) {
    case "ts":
    case "tsx":
      return "#3178c6";
    case "js":
    case "jsx":
      return "#f0db4f";
    case "py":
      return "#3572A5";
    case "json":
      return "#cbcb41";
    case "md":
      return "#519aba";
    case "css":
      return "#519aba";
    case "html":
      return "#e44d26";
    case "yml":
    case "yaml":
      return "#cb171e";
    case "toml":
      return "#9c4221";
    case "sh":
      return "#89e051";
    case "png":
    case "jpg":
    case "jpeg":
    case "gif":
    case "svg":
    case "webp":
    case "ico":
      return "#a074c4";
    case "lock":
      return "#8a8a8a";
    default:
      return "#8a8a8a";
  }
}

// Универсальная иконка-документ, тонируется per-extension цветом.
function DocIcon({ color }: { color: string }) {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      style={{ flexShrink: 0 }}
    >
      <path
        d="M9 1.5H4.5A1.5 1.5 0 0 0 3 3v10a1.5 1.5 0 0 0 1.5 1.5h7A1.5 1.5 0 0 0 13 13V5.5L9 1.5Z"
        fill={color}
        fillOpacity="0.18"
        stroke={color}
        strokeWidth="1"
        strokeLinejoin="round"
      />
      <path d="M9 1.5V5.5H13" stroke={color} strokeWidth="1" strokeLinejoin="round" />
    </svg>
  );
}

function FileIcon({ name }: { name: string }) {
  return (
    <span className="mr-1 inline-flex items-center">
      <DocIcon color={fileIconColor(name)} />
    </span>
  );
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <span className="mr-1 inline-flex items-center">
      <svg
        width="15"
        height="15"
        viewBox="0 0 16 16"
        fill="none"
        aria-hidden="true"
        style={{ flexShrink: 0 }}
      >
        {open ? (
          <path
            d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3l1.5 1.5h5A1.5 1.5 0 0 1 14 5.5v.5H4.2a1 1 0 0 0-.96.73L1.5 12.5V4Z"
            fill={FOLDER_COLOR}
            fillOpacity="0.9"
          />
        ) : (
          <path
            d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3l1.5 1.5h5A1.5 1.5 0 0 1 14 5.5V12a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 1.5 12V4Z"
            fill={FOLDER_COLOR}
            fillOpacity="0.9"
          />
        )}
      </svg>
    </span>
  );
}

interface DirProps {
  projectPath: string;
  canEdit: boolean;
  onOpenFile: (rel: string) => void;
  /** URL для скачивания файла по rel (если доступно). */
  downloadHref?: (rel: string) => string;
  rel: string; // "" для корня
  name: string; // "" для корня
  depth: number;
  defaultOpen?: boolean;
  reloadToken: number;
  onChanged: () => void; // попросить родителя перечитать своих детей
}

function Dir(props: DirProps) {
  const {
    projectPath, canEdit, onOpenFile, downloadHref,
    rel, name, depth, defaultOpen, reloadToken, onChanged,
  } = props;
  const isRoot = rel === "";
  const [open, setOpen] = useState(!!defaultOpen);
  const [entries, setEntries] = useState<FileEntry[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inline, setInline] = useState<Inline>(null);
  const [inlineVal, setInlineVal] = useState("");
  const [localReload, setLocalReload] = useState(0);

  useEffect(() => {
    if (!open && !isRoot) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.filesTree(projectPath, rel).then(
      (r) => {
        if (!cancelled) {
          setEntries(r.entries);
          setTruncated(r.truncated);
        }
      },
      (e) => {
        if (!cancelled) setError((e as Error).message);
      },
    ).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [open, isRoot, projectPath, rel, reloadToken, localReload]);

  async function submitInline() {
    const trimmed = inlineVal.trim();
    if (!trimmed) {
      setInline(null);
      return;
    }
    setError(null);
    try {
      if (inline === "new-file" || inline === "new-dir") {
        const childRel = rel ? `${rel}/${trimmed}` : trimmed;
        await api.createEntry(projectPath, childRel, inline === "new-dir" ? "dir" : "file");
        setLocalReload((t) => t + 1);
      } else if (inline === "rename") {
        const parent = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
        const dst = parent ? `${parent}/${trimmed}` : trimmed;
        await api.moveEntry(projectPath, rel, dst);
        onChanged();
      }
      setInline(null);
      setInlineVal("");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function remove() {
    setError(null);
    try {
      await api.deleteEntry(projectPath, rel);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const pad = { paddingLeft: `${depth * 12 + 8}px` };

  return (
    <div>
      {!isRoot && (
        <div className="group flex items-center gap-1 pr-2 text-sm hover:bg-[var(--bg-hover)]" style={pad}>
          <button
            onClick={() => setOpen((v) => !v)}
            className="flex min-w-0 flex-1 items-center gap-1 py-1 text-left text-[var(--fg-primary)]"
          >
            <ChevronRightIcon
              size={14}
              className={`shrink-0 text-[var(--fg-muted)] transition-transform duration-200 ease-spring ${
                open ? "rotate-90" : ""
              }`}
            />
            <FolderIcon open={open} />
            <span className="truncate">{name}</span>
          </button>
          {canEdit && (
            <span className="hidden gap-1 group-hover:flex">
              <button title="Новый файл" aria-label="Новый файл" className="icon-btn text-[var(--fg-muted)] hover:text-[var(--fg-primary)]" onClick={() => { setOpen(true); setInline("new-file"); }}><PlusIcon size={15} /></button>
              <button title="Новая папка" aria-label="Новая папка" className="icon-btn text-[var(--fg-muted)] hover:text-[var(--fg-primary)]" onClick={() => { setOpen(true); setInline("new-dir"); }}><FolderPlusIcon size={15} /></button>
              <button title="Переименовать" aria-label="Переименовать" className="icon-btn text-[var(--fg-muted)] hover:text-[var(--fg-primary)]" onClick={() => { setInline("rename"); setInlineVal(name); }}><EditIcon size={15} /></button>
              <button title="Удалить" aria-label="Удалить" className="icon-btn text-[var(--fg-muted)] hover:text-red-400" onClick={remove}><TrashIcon size={15} /></button>
            </span>
          )}
        </div>
      )}

      {isRoot && canEdit && (
        <div className="flex gap-3 px-2 py-1 text-xs text-[var(--fg-muted)]">
          <button title="Новый файл" className="inline-flex items-center gap-1 hover:text-[var(--fg-primary)]" onClick={() => setInline("new-file")}><PlusIcon size={13} /> файл</button>
          <button title="Новая папка" className="inline-flex items-center gap-1 hover:text-[var(--fg-primary)]" onClick={() => setInline("new-dir")}><FolderPlusIcon size={13} /> папка</button>
        </div>
      )}

      {(open || isRoot) && (
        <div>
          {inline && (
            <div style={{ paddingLeft: `${(depth + 1) * 12 + 8}px` }} className="flex items-center gap-1 py-1">
              <input
                autoFocus
                value={inlineVal}
                onChange={(e) => setInlineVal(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submitInline();
                  if (e.key === "Escape") { setInline(null); setInlineVal(""); }
                }}
                placeholder={inline === "rename" ? "новое имя" : inline === "new-dir" ? "имя папки" : "имя файла"}
                className="w-40 rounded bg-[var(--bg-input)] px-1 text-sm text-[var(--fg-primary)]"
              />
              <button
                type="button"
                title="Сохранить" aria-label="Сохранить"
                onClick={submitInline}
                className="px-1 text-sm text-[var(--fg-primary)]"
              >
                <CheckIcon size={14} />
              </button>
              <button
                type="button"
                title="Отмена" aria-label="Отмена"
                onClick={() => { setInline(null); setInlineVal(""); }}
                className="px-1 text-sm text-[var(--fg-muted)]"
              >
                <CloseIcon size={14} />
              </button>
            </div>
          )}
          {error && <div style={pad} className="py-1 text-xs text-red-400">{error}</div>}
          {loading && <div style={pad} className="py-1 text-xs text-[var(--fg-muted)]">…</div>}
          {entries?.map((e) =>
            e.type === "dir" ? (
              <Dir
                key={e.rel}
                projectPath={projectPath}
                canEdit={canEdit}
                onOpenFile={onOpenFile}
                downloadHref={downloadHref}
                rel={e.rel}
                name={e.name}
                depth={depth + 1}
                reloadToken={reloadToken}
                onChanged={() => setLocalReload((t) => t + 1)}
              />
            ) : (
              <FileRow
                key={e.rel}
                entry={e}
                depth={depth + 1}
                canEdit={canEdit}
                projectPath={projectPath}
                onOpenFile={onOpenFile}
                downloadHref={downloadHref}
                onChanged={() => setLocalReload((t) => t + 1)}
              />
            ),
          )}
          {truncated && (
            <div
              style={{ paddingLeft: `${(depth + 1) * 12 + 8}px` }}
              className="py-1 text-xs italic text-[var(--fg-muted)]"
            >
              … показаны не все файлы (слишком много)
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function FileRow({
  entry, depth, canEdit, projectPath, onOpenFile, downloadHref, onChanged,
}: {
  entry: FileEntry;
  depth: number;
  canEdit: boolean;
  projectPath: string;
  onOpenFile: (rel: string) => void;
  downloadHref?: (rel: string) => string;
  onChanged: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [val, setVal] = useState(entry.name);
  const [error, setError] = useState<string | null>(null);
  const pad = { paddingLeft: `${depth * 12 + 8}px` };

  async function rename() {
    const t = val.trim();
    if (!t || t === entry.name) {
      setRenaming(false);
      return;
    }
    const parent = entry.rel.includes("/") ? entry.rel.slice(0, entry.rel.lastIndexOf("/")) : "";
    const dst = parent ? `${parent}/${t}` : t;
    setError(null);
    try {
      await api.moveEntry(projectPath, entry.rel, dst);
      setRenaming(false);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function remove() {
    setError(null);
    try {
      await api.deleteEntry(projectPath, entry.rel);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (renaming) {
    return (
      <div style={pad} className="flex items-center gap-1 py-1">
        <input
          autoFocus
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") rename();
            if (e.key === "Escape") { setVal(entry.name); setRenaming(false); }
          }}
          className="w-40 rounded bg-[var(--bg-input)] px-1 text-sm text-[var(--fg-primary)]"
        />
        <button
          type="button"
          title="Сохранить"
          onClick={rename}
          className="px-1 text-sm text-[var(--fg-primary)]"
        >
          <CheckIcon size={14} />
        </button>
        <button
          type="button"
          title="Отмена"
          onClick={() => { setVal(entry.name); setRenaming(false); }}
          className="px-1 text-sm text-[var(--fg-muted)]"
        >
          <CloseIcon size={14} />
        </button>
        {error && <span className="ml-2 text-xs text-red-400">{error}</span>}
      </div>
    );
  }

  return (
    <div
      className="group flex items-center gap-1 pr-2 text-sm hover:bg-[var(--bg-hover)]"
      style={pad}
    >
      <button
        onClick={() => onOpenFile(entry.rel)}
        className="flex min-w-0 flex-1 items-center py-1 text-left text-[var(--fg-primary)]"
      >
        <FileIcon name={entry.name} />
        <span className="truncate">{entry.name}</span>
      </button>
      <span className="hidden items-center gap-1 group-hover:flex">
        {downloadHref && (
          <a
            href={downloadHref(entry.rel)}
            download={entry.name}
            title="Скачать"
            aria-label="Скачать"
            className="icon-btn text-[var(--fg-muted)] hover:text-[var(--fg-primary)]"
            onClick={(e) => e.stopPropagation()}
          >
            <DownloadIcon size={15} />
          </a>
        )}
        {canEdit && (
          <>
            <button title="Переименовать" aria-label="Переименовать" className="icon-btn text-[var(--fg-muted)] hover:text-[var(--fg-primary)]" onClick={() => { setVal(entry.name); setRenaming(true); }}><EditIcon size={15} /></button>
            <button title="Удалить" aria-label="Удалить" className="icon-btn text-[var(--fg-muted)] hover:text-red-400" onClick={remove}><TrashIcon size={15} /></button>
          </>
        )}
      </span>
    </div>
  );
}

export default function FileTree({
  projectPath,
  canEdit,
  onOpenFile,
  downloadHref,
  reloadToken,
}: {
  projectPath: string;
  canEdit: boolean;
  onOpenFile: (rel: string) => void;
  downloadHref?: (rel: string) => string;
  reloadToken: number;
}) {
  return (
    <Dir
      projectPath={projectPath}
      canEdit={canEdit}
      onOpenFile={onOpenFile}
      downloadHref={downloadHref}
      rel=""
      name=""
      depth={0}
      defaultOpen
      reloadToken={reloadToken}
      onChanged={() => {}}
    />
  );
}
