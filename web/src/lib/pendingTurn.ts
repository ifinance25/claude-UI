import type { HistoryMessage } from "@/lib/types";

/**
 * request_id хода, который на момент загрузки истории ещё НЕ завершён.
 *
 * Зачем: генерация переживает уход из диалога (см. routes_ws.py), но пока
 * Claude думает, в историю попадают только вопрос и служебные строки — текста
 * ещё нет. Вернувшийся пользователь видел свой вопрос, пустоту под ним и НИ
 * ОДНОГО признака работы: чат выглядел сброшенным, будто историю стёрли.
 * По этому признаку чат возвращает индикатор «Claude думает…».
 *
 * Ход считается незавершённым, если для его request_id не пришло ни одного
 * события `finished` — именно его публикует бэкенд и на успехе, и на стопе.
 */
export function pendingRequestId(history: HistoryMessage[]): string | null {
  const finished = new Set<string>();
  let lastRequestId: string | null = null;
  for (const m of history) {
    if (!m.request_id) continue;
    if (m.type === "finished") {
      finished.add(m.request_id);
      continue;
    }
    lastRequestId = m.request_id;
  }
  if (!lastRequestId || finished.has(lastRequestId)) return null;
  return lastRequestId;
}
