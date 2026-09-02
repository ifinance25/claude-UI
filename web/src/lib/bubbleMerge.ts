/** Куда дописывать очередной кусок текста ответа.
 *
 * Регрессия, ради которой это вынесено отдельно: склейка дельт смотрела только
 * на ПОСЛЕДНИЙ бабл ленты. Если между двумя кусками текста проскакивало
 * служебное событие (лог, init, счётчик usage, шаг субагента), текст уходил в
 * НОВЫЙ бабл — и один ответ рисовался двумя markdown-блоками, иногда разрывая
 * слово: «М», а на следующей строке «ы в demo-project».
 */

/** Минимум полей ленты, нужный для решения о склейке. */
export interface MergeBubble {
  kind: string;
  requestId?: string | null;
}

/** События, прозрачные для склейки: они не являются границей текста ответа. */
export const MERGE_TRANSPARENT_KINDS = new Set([
  "log",
  "init",
  "usage",
  "subagent_start",
  "subagent_log",
  "subagent_finish",
]);

/**
 * Индекс text-бабла того же хода, к которому дописывается дельта; -1 — нужен новый.
 *
 * Идём с конца, перешагивая прозрачные события. Первый же НЕпрозрачный бабл
 * (tool_use, ошибка, сообщение пользователя) — настоящая граница: текст до и
 * после вызова инструмента должен остаться разными блоками.
 */
export function mergeTargetIndex(
  list: readonly MergeBubble[],
  requestId?: string | null,
): number {
  for (let i = list.length - 1; i >= 0; i--) {
    const b = list[i];
    if (b.kind === "text") return b.requestId === requestId ? i : -1;
    if (!MERGE_TRANSPARENT_KINDS.has(b.kind)) return -1;
  }
  return -1;
}
