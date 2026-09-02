// Shared usage-metadata parsing. Single source of truth for turning a
// raw usage/metadata object into token + cost numbers — used by the
// header UsageBadge accumulator (Chat) and the per-turn UsageEvent plate.
//
// The backend sends usage in TWO shapes:
//   1. AgentFinished.usage — flat: { input_tokens, output_tokens,
//      cache_read_tokens, cache_creation_tokens, cost_usd }.
//   2. streaming_update kind=usage — nested: { usage: { input_tokens,
//      cache_read_input_tokens, ... }, total_cost_usd, ... } where tokens
//      live under .usage and the cost lives at the top level.
// We must handle both, or the badge shows "0 токенов" with a non-zero
// cost (tokens were looked up on the wrong level).

export interface UsageBreakdown {
  input: number;
  output: number;
  cacheRead: number;
  cacheCreation: number;
  costUsd: number;
}

export interface UsageTotals {
  tokens: number;
  costUsd: number;
}

/** Coerce to a finite number; anything non-numeric/NaN/Infinity → 0. */
export function toFinite(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Extract token/cost fields, tolerant of both the flat and the nested
 * usage shapes (see file header). Cache tokens may arrive under either
 * `cache_read_tokens` or Claude's `cache_read_input_tokens` — one of the
 * two is 0, so we take the larger.
 */
export function usageBreakdown(
  meta: Record<string, unknown> | null | undefined
): UsageBreakdown {
  const m = meta ?? {};
  const nested =
    m.usage && typeof m.usage === "object"
      ? (m.usage as Record<string, unknown>)
      : m;

  const input = toFinite(nested.input_tokens);
  const output = toFinite(nested.output_tokens);
  const cacheRead = Math.max(
    toFinite(nested.cache_read_tokens),
    toFinite(nested.cache_read_input_tokens)
  );
  const cacheCreation = Math.max(
    toFinite(nested.cache_creation_tokens),
    toFinite(nested.cache_creation_input_tokens)
  );
  // Cost sits at the top level for the nested shape (total_cost_usd) and
  // inside the flat usage object for AgentFinished (cost_usd).
  const costUsd = toFinite(
    m.cost_usd ?? m.total_cost_usd ?? nested.cost_usd ?? nested.total_cost_usd
  );
  return { input, output, cacheRead, cacheCreation, costUsd };
}

/** Token total + cost, for the header badge accumulator. */
export function parseUsage(
  meta: Record<string, unknown> | null | undefined
): UsageTotals {
  const b = usageBreakdown(meta);
  return {
    tokens: b.input + b.output + b.cacheRead + b.cacheCreation,
    costUsd: b.costUsd,
  };
}
