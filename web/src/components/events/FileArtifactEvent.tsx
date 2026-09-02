import { api } from "@/api/client";
import { FileIcon } from "@/components/icons";

interface Props {
  sessionUuid: string;
  filePath: string;
  action: "Write" | "Edit";
}

export function FileArtifactEvent({ sessionUuid, filePath, action }: Props) {
  const name = filePath.split("/").pop() || filePath;
  const verb = action === "Write" ? "создан" : "изменён";
  return (
    <div className="my-2 flex items-center justify-between gap-3 rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]/60 px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <FileIcon size={20} className="shrink-0 text-[var(--fg-muted)]" />
        <div className="min-w-0">
          <div className="truncate text-sm text-[var(--fg-primary)]" title={filePath}>
            {name}
          </div>
          <div className="text-xs text-[var(--fg-muted)]">Файл {verb} Claude</div>
        </div>
      </div>
      <a
        href={api.sessionFileUrl(sessionUuid, filePath)}
        download={name}
        className="shrink-0 rounded-xl bg-[var(--accent)] px-3 py-1.5 text-xs font-medium text-[var(--bg-canvas)] hover:opacity-90"
      >
        Скачать
      </a>
    </div>
  );
}
