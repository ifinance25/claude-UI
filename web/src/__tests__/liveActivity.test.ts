import { describe, it, expect } from "vitest";
import {
  activityFromEvent,
  displayVerb,
  THINKING_VERBS,
  type LiveActivity,
} from "@/lib/liveActivity";

describe("liveActivity", () => {
  it("thinking-дельта → фаза thinking без контекста", () => {
    expect(activityFromEvent("thinking", "часть", {})).toEqual({
      phase: "thinking",
      context: "",
    });
  });

  it("tool_use Read → фаза tool, контекст из строки 📂 (а не из 🔧 Name)", () => {
    const content = "🔧 Read\n   📂 web/src/components/Chat.tsx";
    expect(activityFromEvent("tool_use", content, { name: "Read" })).toEqual({
      phase: "tool",
      toolName: "Read",
      context: "web/src/components/Chat.tsx",
    });
  });

  it("tool_use Bash → контекст из команды $", () => {
    const content = "🔧 Bash\n   $ npm test";
    expect(activityFromEvent("tool_use", content, { name: "Bash" })?.context).toBe(
      "npm test",
    );
  });

  it("tool_use Bash с описанием → контекст из 💬 (Claude Code шлёт description)", () => {
    const content = "🔧 Bash\n   💬 запустить тесты";
    expect(activityFromEvent("tool_use", content, { name: "Bash" })?.context).toBe(
      "запустить тесты",
    );
  });

  it("tool_use Grep → контекст из паттерна 🔍", () => {
    const content = "🔧 Grep\n   🔍 thinking\n   📂 src";
    expect(activityFromEvent("tool_use", content, { name: "Grep" })?.context).toBe(
      "thinking",
    );
  });

  it("metadata.file_path как фолбэк, обрезка длинного", () => {
    const long = "a/".repeat(40) + "file.ts";
    const ctx = activityFromEvent("tool_use", "🔧 Edit", {
      name: "Edit",
      file_path: long,
    })?.context;
    expect(ctx).toContain("file.ts");
    expect((ctx as string).length).toBeLessThanOrEqual(40);
  });

  it("Skill → null (служебный вызов не меняет индикатор)", () => {
    expect(activityFromEvent("tool_use", "🔧 Skill", { name: "Skill" })).toBeNull();
  });

  it("видимый text → typing; пустой/только-мысли text → null (фазу не меняет)", () => {
    expect(activityFromEvent("text", "Привет", {})).toEqual({
      phase: "typing",
      context: "",
    });
    const OPEN = "\x01THINKING\x02";
    const CLOSE = "\x02THINKING\x01";
    expect(activityFromEvent("text", `${OPEN}x${CLOSE}`, {})).toBeNull();
  });

  it("log/init/subagent → null (заголовок не меняют)", () => {
    expect(activityFromEvent("log", "что-то", {})).toBeNull();
    expect(activityFromEvent("subagent_log", "что-то", {})).toBeNull();
  });

  it("displayVerb: tool→по инструменту, typing→Печатает, thinking→ротация", () => {
    const tool: LiveActivity = { phase: "tool", toolName: "Grep", context: "x" };
    expect(displayVerb(tool, 0, false)).toBe("Ищет");
    const typing: LiveActivity = { phase: "typing", context: "" };
    expect(displayVerb(typing, 5, false)).toBe("Печатает");
    const think: LiveActivity = { phase: "thinking", context: "" };
    expect(displayVerb(think, 1, false)).toBe(THINKING_VERBS[1]);
    expect(displayVerb(think, 3, true)).toBe(THINKING_VERBS[0]);
  });
});
