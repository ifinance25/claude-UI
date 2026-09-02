// Зоны заполнения контекстного окна для индикатора. Единое место порогов,
// чтобы веб-цвет и логика подсказки не разъезжались.
export type CtxZone = "green" | "amber" | "red";

export const CTX_AMBER_PCT = 70;
export const CTX_RED_PCT = 85;

export function contextZone(pct: number): CtxZone {
  if (pct >= CTX_RED_PCT) return "red";
  if (pct >= CTX_AMBER_PCT) return "amber";
  return "green";
}
