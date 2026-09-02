import { providerLogo } from "@/components/icons";

interface Props {
  projectName: string;
  modelLabel?: string;
  modelId?: string;
  /** Проект не подключён — сессия уйдёт во временную папку. */
  projectMissing?: boolean;
}

export default function ChatEmptyState({ projectName, modelLabel, modelId, projectMissing }: Props) {
  return (
    <div className="flex animate-riseIn flex-col items-center px-6 pb-6">
      <div className="mb-6 flex h-20 w-20 animate-iconPop items-center justify-center rounded-full bg-[var(--bg-hover)] text-[var(--fg-primary)]">
        {providerLogo(modelId, 40)}
      </div>
      <div className="font-display text-3xl font-semibold text-[var(--fg-primary)]">{projectName}</div>
      <div className="mt-2 text-lg text-[var(--fg-muted)]">Чем я могу помочь?</div>
      {projectMissing && (
        // На свежей установке PROJECTS_DIR пуст. Сессия без проекта работает
        // во временной папке data/scratch, и человек об этом никак не узнавал:
        // подсказка «нет проектов» жила в секции проектов сайдбара, а её в
        // light вырезали.
        <div className="mt-4 max-w-md rounded-lg border border-[var(--border)] bg-[var(--bg-hover)] px-4 py-3 text-sm text-[var(--fg-muted)]">
          Проект не подключён — Claude работает во временной папке{" "}
          <code>data/scratch</code>. Положите папку проекта внутрь{" "}
          <code>PROJECTS_DIR</code> и перезапустите сервис:{" "}
          <code>systemctl restart vels-claude</code>.
        </div>
      )}
      {modelLabel && (
        <div className="mt-3 flex items-center gap-2 rounded-full bg-[var(--bg-hover)] px-3 py-1 text-sm text-[var(--fg-secondary)]">
          {providerLogo(modelId, 16)} {modelLabel}
        </div>
      )}
    </div>
  );
}
