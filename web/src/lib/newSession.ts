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

/**
 * Что делать по нажатию «Новый чат» в light-версии.
 *
 * Проект здесь ровно один, и брать его надо из загруженного списка. Пока
 * список не пришёл, `projects` пуст — и неотличим от «проектов нет». Раньше
 * кнопка в обоих случаях создавала сессию с project_path=null: Claude уходил
 * работать в data/scratch, а привязать проект такому чату уже нельзя. Один
 * клик до конца загрузки — и работа человека уезжала во временную папку.
 */
export type NewChatDecision =
  | { action: "wait" }
  | { action: "no-projects" }
  | { action: "create"; project: { path: string; name: string } };

export function decideNewChat(
  projects: { path: string; name: string }[],
  projectsLoaded: boolean,
): NewChatDecision {
  if (!projectsLoaded) return { action: "wait" };
  if (projects.length === 0) return { action: "no-projects" };
  return { action: "create", project: projects[0] };
}
