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
  current: "claude-sonnet-5",
  permission_mode: "default",
  known: [
    {
      id: "claude-fable-5",
      label: "Claude Fable 5",
      hint: "Самая мощная — для самых сложных задач",
    },
    {
      id: "claude-opus-5",
      label: "Claude Opus 5",
      hint: "Сильная — флагман для кода и агентных задач",
    },
    {
      id: "claude-sonnet-5",
      label: "Claude Sonnet 5",
      hint: "Сбалансированная — рабочая лошадка (значение по умолчанию)",
    },
    {
      id: "claude-haiku-4-5-20251001",
      label: "Claude Haiku 4.5",
      hint: "Быстрая и дешёвая — для лёгких задач",
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
