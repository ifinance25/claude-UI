export function LogEvent({ content }: { content: string }) {
  return (
    <pre className="my-1.5 overflow-x-auto rounded-lg bg-[var(--bg-sidebar)]/60 px-4 py-2.5 font-mono text-[13px] leading-relaxed text-[var(--fg-muted)]">
      {content}
    </pre>
  );
}
