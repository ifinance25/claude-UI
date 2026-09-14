import { api } from "@/api/client";
import type { Session } from "@/lib/types";

/**
 * Создаёт новую сессию (в указанном проекте или без) и выбирает её через
 * onSelect. Общая точка «Нового чата» — используется из ChatPage (кнопка в
 * индикаторе контекста). Возвращает созданную сессию.
 */
export async function startNewSession(
  project: { path: string | null; name: string | null } | null,
  onSelect: (s: Session) => void,
): Promise<Session> {
  const s = await api.createSession(project?.path ?? null, project?.name ?? null);
  onSelect(s);
  return s;
}
