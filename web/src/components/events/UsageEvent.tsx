import { contextWindowFor } from "@/lib/modelContext";
import { usageBreakdown } from "@/lib/usage";

interface Props {
  usage: Record<string, unknown>;
  /** Модель хода — чтобы посчитать % использованного контекстного окна. */
  modelId?: string;
}

export function UsageEvent({ usage, modelId }: Props) {
  // usageBreakdown — общий парсер, понимающий и плоский (finished), и
  // вложенный (streaming kind=usage) формат + Claude'овские
  // cache_*_input_tokens; NaN-safe (CR3-10/20).
  const { input, output, cacheRead, cacheCreation, costUsd: cost } =
    usageBreakdown(usage);
  const total = input + output + cacheRead + cacheCreation;

  // Контекст за ход = вход + кэш (то, что Claude «держал» в окне).
  const contextTokens = input + cacheRead + cacheCreation;
  const window = contextWindowFor(modelId);
  const ctxPct =
    window > 0 ? Math.min(100, Math.round((contextTokens / window) * 100)) : 0;

  // Время ответа прокидывается из события finished (Chat патчит метадату
  // usage-бабла ключом __elapsed_ms — см. addLiveEvent).
  const elapsedRaw = usage["__elapsed_ms"];
  const elapsedMs =
    typeof elapsedRaw === "number" && Number.isFinite(elapsedRaw)
      ? elapsedRaw
      : null;

  return (
    <div className="my-3 flex flex-wrap items-center gap-x-5 gap-y-1 rounded-xl bg-[var(--bg-sidebar)]/40 px-4 py-2.5 text-[13px] tabular-nums text-[var(--fg-muted)]">
      <span>tokens: {total.toLocaleString()}</span>
      <span>in/out: {input.toLocaleString()}/{output.toLocaleString()}</span>
      {(cacheRead > 0 || cacheCreation > 0) && (
        <span>cache: {cacheRead.toLocaleString()}/{cacheCreation.toLocaleString()}</span>
      )}
      {window > 0 && contextTokens > 0 && (
        <span title={`${contextTokens.toLocaleString()} / ${window.toLocaleString()}`}>
          контекст {ctxPct}%
        </span>
      )}
      {elapsedMs != null && <span>{(elapsedMs / 1000).toFixed(1)}с</span>}
      <span className="ml-auto font-semibold text-[var(--fg-secondary)]">
        ${cost.toFixed(4)}
      </span>
    </div>
  );
}
