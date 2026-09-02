// Окно контекста по id модели. Сейчас у всех 1M (решение босса 2026-07-02);
// вынесено отдельно, чтобы менять в одном месте при появлении моделей с
// другим окном.
const CONTEXT_WINDOWS: Record<string, number> = {
  "claude-fable-5": 1_000_000,
  "claude-opus-5": 1_000_000,
  "claude-sonnet-5": 1_000_000,
  "claude-haiku-4-5-20251001": 1_000_000,
};
const DEFAULT_WINDOW = 1_000_000;

export function contextWindowFor(modelId: string | undefined): number {
  if (!modelId) return DEFAULT_WINDOW;
  return CONTEXT_WINDOWS[modelId] ?? DEFAULT_WINDOW;
}
