import { hasVisibleText } from "@/lib/stripThinking";
import type { StreamingUpdateKind } from "@/lib/types";

export type Phase = "thinking" | "tool" | "typing";

export interface LiveActivity {
  phase: Phase;
  /** Короткий ярлык цели последнего действия (для фазы tool). */
  context: string;
  /** Имя инструмента (для фазы tool) — из metadata.name. */
  toolName?: string;
}

export const THINKING_VERBS = ["Думает", "Размышляет", "Соображает", "Обдумывает"];

const TOOL_VERBS: Record<string, string> = {
  read: "Изучает",
  grep: "Ищет",
  glob: "Ищет",
  bash: "Выполняет",
  write: "Пишет",
  edit: "Редактирует",
  multiedit: "Редактирует",
  webfetch: "Смотрит в сети",
  websearch: "Смотрит в сети",
  fetch: "Смотрит в сети",
  task: "Запускает агента",
  agent: "Запускает агента",
};

export function verbForTool(name: string): string {
  return TOOL_VERBS[name.trim().toLowerCase()] ?? "Работает";
}

function clip(s: string, max = 40): string {
  const t = s.trim();
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
}

function basename(path: string): string {
  const parts = path.split(/[/\\]/);
  return parts[parts.length - 1] || path;
}

/**
 * Достаёт цель действия из форматированного `content` (bridge.py _format_tool_params).
 * Первая строка — заголовок «🔧 Name», цель — на последующих строках за
 * emoji-маркерами (📂/📝 путь, `$ ` команда, 🔍 паттерн, 💬 описание Bash). Эмодзи — суррогатные
 * пары (.length === 2), снимаем по реальной длине через .slice(2).
 */
export function toolContext(content: string, metadata: Record<string, unknown>): string {
  const lines = content
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .slice(1);
  for (const raw of lines) {
    if (raw.startsWith("📂") || raw.startsWith("📝") || raw.startsWith("🔍") || raw.startsWith("💬")) {
      return clip(raw.slice(2).trim());
    }
    if (raw.startsWith("$ ")) {
      return clip(raw.slice(2).trim());
    }
  }
  const fp = metadata?.file_path;
  if (typeof fp === "string" && fp) return clip(basename(fp));
  return "";
}

/**
 * Строит LiveActivity из стрим-события активного запроса, либо null, если
 * событие не должно менять «заголовок» индикатора (log/init/subagent/служебный
 * Skill/текст-только-из-мыслей).
 */
export function activityFromEvent(
  kind: StreamingUpdateKind,
  content: string,
  metadata: Record<string, unknown>,
): LiveActivity | null {
  if (kind === "thinking") return { phase: "thinking", context: "" };
  if (kind === "tool_use") {
    const name = String(metadata?.name ?? "");
    if (name === "Skill") return null;
    return { phase: "tool", toolName: name, context: toolContext(content, metadata) };
  }
  if (kind === "text") {
    return hasVisibleText(content) ? { phase: "typing", context: "" } : null;
  }
  return null;
}

/** Глагол для отображения. Ротация — только для фазы thinking и только без reduced-motion. */
export function displayVerb(
  activity: LiveActivity,
  thinkingIndex: number,
  reduced: boolean,
): string {
  if (activity.phase === "tool") return verbForTool(activity.toolName ?? "");
  if (activity.phase === "typing") return "Печатает";
  const i = reduced ? 0 : ((thinkingIndex % THINKING_VERBS.length) + THINKING_VERBS.length) % THINKING_VERBS.length;
  return THINKING_VERBS[i];
}
