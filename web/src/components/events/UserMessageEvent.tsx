interface Props {
  content: string;
  source?: "telegram" | "web" | "webhook" | "cron";
}

export function UserMessageEvent({ content, source }: Props) {
  const tag =
    source === "telegram" ? "из Telegram" :
    source === "webhook" ? "из вебхука" :
    source === "cron" ? "из планировщика" :
    null;
  return (
    <div className="my-4 flex justify-end">
      <div className="max-w-[80%] rounded-3xl rounded-br-lg bg-[var(--bg-input)] px-5 py-3 text-[15px] text-[var(--fg-primary)]">
        {tag && (
          <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
            {tag}
          </div>
        )}
        <div className="whitespace-pre-wrap leading-relaxed">{content}</div>
      </div>
    </div>
  );
}
