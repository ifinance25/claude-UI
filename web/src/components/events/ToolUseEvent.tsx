import { useState } from "react";
import { ChevronRightIcon, FileIcon } from "@/components/icons";

interface Props {
  /** Multi-line content from bridge.py: first line is "🔧 ToolName",
   * subsequent lines are formatted params ("   📂 path/file",
   * "   $ ls -la", "   ✏️ old", "   → new", "   💬 description"). */
  name: string;
  metadata: Record<string, unknown>;
}

/** Strip the leading wrench + return clean tool name from "🔧 Bash" */
function extractToolName(firstLine: string, fallback?: string): string {
  const cleaned = firstLine.replace(/^🔧\s*/, "").trim();
  if (cleaned) return cleaned;
  return fallback || "tool";
}

/** Choose a tint + label for known tool kinds. Falls back to neutral. */
function styleFor(tool: string): {
  tint: string;
  badge: string;
  label: string;
} {
  const t = tool.toLowerCase();
  if (t === "bash") return { tint: "text-emerald-400", badge: "bg-emerald-500/10", label: "bash" };
  if (t === "read") return { tint: "text-sky-400", badge: "bg-sky-500/10", label: "read" };
  if (t === "write") return { tint: "text-violet-400", badge: "bg-violet-500/10", label: "write" };
  if (t === "edit" || t === "multiedit") return { tint: "text-amber-400", badge: "bg-amber-500/10", label: "edit" };
  if (t === "grep") return { tint: "text-fuchsia-400", badge: "bg-fuchsia-500/10", label: "grep" };
  if (t === "glob") return { tint: "text-pink-400", badge: "bg-pink-500/10", label: "glob" };
  if (t === "webfetch" || t === "fetch") return { tint: "text-cyan-400", badge: "bg-cyan-500/10", label: "fetch" };
  if (t === "websearch") return { tint: "text-cyan-400", badge: "bg-cyan-500/10", label: "search" };
  if (t === "task" || t === "agent") return { tint: "text-orange-400", badge: "bg-orange-500/10", label: "agent" };
  return { tint: "text-[var(--fg-secondary)]", badge: "bg-[var(--bg-hover)]", label: tool.toLowerCase() };
}

interface ParsedLine {
  kind: "path" | "shell" | "edit-old" | "edit-new" | "desc" | "text";
  body: string;
}

function parseLine(raw: string): ParsedLine {
  const trimmed = raw.replace(/^\s+/, "");
  // ВАЖНО: эмодзи 📂/📝/💬 — суррогатные пары UTF-16 (.length === 2).
  // Резать их через .slice(1) нельзя — останется «половина» символа и
  // вывод превращается в мусор. Снимаем префикс по его реальной длине.
  // (Эти эмодзи — МАРКЕРЫ из bridge.py, детектятся, но не показываются:
  //  путь рендерится иконкой, edit — знаками -/+.)
  for (const [emoji, kind] of [
    ["📂", "path"],
    ["📝", "path"],
    ["💬", "desc"],
  ] as const) {
    if (trimmed.startsWith(emoji)) {
      return { kind, body: trimmed.slice(emoji.length).trim() };
    }
  }
  if (trimmed.startsWith("$ ")) {
    return { kind: "shell", body: trimmed.slice(2).trim() };
  }
  if (trimmed.startsWith("✏️")) {
    return { kind: "edit-old", body: trimmed.slice("✏️".length).trim() };
  }
  if (trimmed.startsWith("→")) {
    return { kind: "edit-new", body: trimmed.slice("→".length).trim() };
  }
  return { kind: "text", body: trimmed };
}

function LineRow({ line }: { line: ParsedLine }) {
  if (line.kind === "shell") {
    return (
      <div className="flex items-baseline gap-2 font-mono text-[13px] text-[var(--fg-primary)]">
        <span className="select-none text-[var(--fg-muted)]">$</span>
        <span className="break-all">{line.body}</span>
      </div>
    );
  }
  if (line.kind === "path") {
    return (
      <div className="flex items-center gap-2 font-mono text-[13px] text-[var(--fg-secondary)]">
        <FileIcon size={13} className="shrink-0 text-[var(--fg-muted)]" />
        <span className="break-all">{line.body}</span>
      </div>
    );
  }
  if (line.kind === "edit-old") {
    return (
      <div className="flex items-baseline gap-2 font-mono text-[13px] text-red-300/90">
        <span className="select-none text-[var(--fg-muted)]">-</span>
        <span className="break-all">{line.body}</span>
      </div>
    );
  }
  if (line.kind === "edit-new") {
    return (
      <div className="flex items-baseline gap-2 font-mono text-[13px] text-emerald-300/90">
        <span className="select-none text-[var(--fg-muted)]">+</span>
        <span className="break-all">{line.body}</span>
      </div>
    );
  }
  if (line.kind === "desc") {
    return (
      <div className="text-sm text-[var(--fg-secondary)] italic">{line.body}</div>
    );
  }
  return <div className="text-sm text-[var(--fg-secondary)]">{line.body}</div>;
}

export function ToolUseEvent({ name, metadata }: Props) {
  const [open, setOpen] = useState(false);

  // `name` here is actually the formatted multi-line `content` from
  // bridge.py. metadata.name has the bare tool name.
  const lines = name.split("\n").filter((l) => l.trim().length > 0);
  const header = lines[0] || "";
  const rest = lines.slice(1).map(parseLine);
  const toolName = extractToolName(header, String(metadata?.name ?? ""));
  const { tint, badge, label } = styleFor(toolName);

  const hasDetails = rest.length > 0;
  const previewLine = rest.find((l) => l.kind !== "text") || rest[0];

  return (
    <div className="my-2 overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]/60">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={!hasDetails}
        className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-[var(--fg-secondary)] transition-colors hover:bg-[var(--bg-hover)]/50 disabled:cursor-default"
      >
        {hasDetails ? (
          <ChevronRightIcon
            size={16}
            className={`shrink-0 transition-transform duration-200 ease-spring ${
              open ? "rotate-90" : ""
            }`}
          />
        ) : (
          <span className="inline-block w-4" />
        )}
        <span className={`rounded-md px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider ${badge} ${tint}`}>
          {label}
        </span>
        <span className={`flex-1 truncate font-mono text-[13px] ${tint}`}>
          {toolName}
        </span>
        {!open && previewLine && (
          <span className="truncate text-[13px] text-[var(--fg-muted)]">
            {previewLine.body.slice(0, 80)}
          </span>
        )}
      </button>
      {open && hasDetails && (
        <div className="space-y-1.5 border-t border-[var(--border-subtle)] bg-[var(--bg-canvas)]/40 px-5 py-3">
          {rest.map((line, i) => (
            <LineRow key={i} line={line} />
          ))}
        </div>
      )}
    </div>
  );
}
