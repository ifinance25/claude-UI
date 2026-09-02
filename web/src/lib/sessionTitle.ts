import type { Session } from "@/lib/types";

const MONTHS = ["янв","фев","мар","апр","мая","июн","июл","авг","сен","окт","ноя","дек"];

/** Заголовок чата: ручные notes имеют приоритет; иначе «Новый чат · DD mon HH:MM». */
export function sessionTitle(session: Pick<Session, "notes" | "created_at">): string {
  const n = session.notes?.trim();
  if (n) return n;
  const d = new Date(session.created_at);
  if (isNaN(d.getTime())) return "Новый чат";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `Новый чат · ${d.getDate()} ${MONTHS[d.getMonth()]} ${hh}:${mm}`;
}

const TITLE_MAX = 40;

/**
 * Авто-заголовок чата из первого сообщения: первая строка, обрезанная по
 * ~40 символам (с «…» если урезали). Возвращает "" если осмысленного
 * текста нет — вызывающий не должен сохранять пустой заголовок.
 */
export function deriveTitle(text: string): string {
  const firstLine = (text.split("\n").find((l) => l.trim()) ?? "").trim();
  if (!firstLine) return "";
  if (firstLine.length <= TITLE_MAX) return firstLine;
  return firstLine.slice(0, TITLE_MAX).trimEnd() + "…";
}
