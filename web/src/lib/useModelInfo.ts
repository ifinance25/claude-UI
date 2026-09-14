import { useContext } from "react";
import { ModelInfoContext, type UseModelInfoResult } from "@/lib/ModelInfoContext";
import type { ModelInfo } from "@/lib/types";

/**
 * Дефолт на случай, если ``GET /api/model`` упал (бэк ещё не
 * перезапущен после правок или сеть отвалилась) — показываем понятный
 * список вместо мёртвой кнопки. Это ЕДИНСТВЕННОЕ место правды для
 * fallback'а, чтобы не плодить копии по компонентам.
 */
export const FALLBACK_MODEL_INFO: ModelInfo = {
  current: "claude-sonnet-4-6",
  permission_mode: "default",
  known: [
    {
      id: "claude-sonnet-4-6",
      label: "Claude Sonnet 4.6",
      hint: "Бэк недоступен — показано значение по умолчанию",
    },
    {
      id: "claude-opus-4-8",
      label: "Claude Opus 4.8",
      hint: "Самый сильный — для сложных задач",
    },
    {
      id: "claude-haiku-4-5-20251001",
      label: "Claude Haiku 4.5",
      hint: "Быстрый и дешёвый — для лёгких задач",
    },
  ],
};

export type { UseModelInfoResult };

/**
 * Тонкая обёртка над ModelInfoContext — все компоненты разделяют
 * один state, fetch выполняется один раз на дереве, тест-изоляция
 * работает (контекст создаётся отдельно в каждом render-цикле, без
 * module-level mutable Set'ов).
 *
 * ``useFallback`` оставлен ради обратной совместимости. Если бэк
 * недоступен и контекст не отдал info — возвращаем fallback вместо
 * `null`, чтобы кнопка не была мёртвой.
 */
export function useModelInfo(useFallback = false): UseModelInfoResult {
  const ctx = useContext(ModelInfoContext);
  if (ctx === null) {
    throw new Error(
      "useModelInfo must be used inside <ModelInfoProvider>",
    );
  }
  if (useFallback && ctx.info === null && ctx.error !== null) {
    return { ...ctx, info: FALLBACK_MODEL_INFO };
  }
  return ctx;
}
