interface Props {
  kind: string;
  content: string;
}

const LABEL: Record<string, string> = {
  subagent_start: "субагент запущен",
  subagent_log: "субагент",
  subagent_finish: "субагент завершён",
};

export function SubagentEvent({ kind, content }: Props) {
  return (
    <div className="my-1.5 flex gap-3 rounded-lg border-l-2 border-violet-500/60 bg-[var(--bg-sidebar)]/40 px-4 py-2.5 text-sm">
      <span className="font-semibold text-violet-400">{LABEL[kind] ?? kind}</span>
      {content && (
        <span className="text-[var(--fg-secondary)]">{content}</span>
      )}
    </div>
  );
}
