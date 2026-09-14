interface Props {
  tokens: number;
  costUsd: number;
}

/** Компактный бейдж в шапке: суммарные за сессию токены + расход.
 * Контекст %, время и побюджетные данные перенесены в подвал каждого
 * сообщения (UsageEvent) по просьбе пользователя. */
export default function UsageBadge({ tokens, costUsd }: Props) {
  if (tokens === 0 && costUsd === 0) return null;
  const human = tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens);
  return (
    <div
      className="hidden items-center gap-2 rounded-xl bg-[var(--bg-hover)]/60 px-3 py-1.5 text-xs tabular-nums text-[var(--fg-secondary)] sm:flex"
      title={`${tokens.toLocaleString()} токенов · $${costUsd.toFixed(4)} за сессию`}
    >
      <span>{human} токенов</span>
      <span className="text-[var(--fg-muted)]">·</span>
      <span>${costUsd.toFixed(3)}</span>
    </div>
  );
}
