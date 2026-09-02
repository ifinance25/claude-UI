import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ThinkingIndicator from "@/components/ThinkingIndicator";
import type { LiveActivity } from "@/lib/liveActivity";

const tool: LiveActivity = { phase: "tool", toolName: "Read", context: "Chat.tsx" };

describe("ThinkingIndicator", () => {
  it("показывает глагол по инструменту и контекст", () => {
    render(
      <ThinkingIndicator activity={tool} elapsedMs={8000} open={false} onToggle={() => {}} panelId="p1" />,
    );
    expect(screen.getByText(/Изучает/)).toBeTruthy();
    expect(screen.getByText(/Chat\.tsx/)).toBeTruthy();
  });

  it("тоггл — кнопка с aria-expanded, клик зовёт onToggle", () => {
    const onToggle = vi.fn();
    render(
      <ThinkingIndicator activity={tool} elapsedMs={0} open={true} onToggle={onToggle} panelId="p1" />,
    );
    const btn = screen.getByRole("button");
    expect(btn.getAttribute("aria-expanded")).toBe("true");
    expect(btn.getAttribute("aria-controls")).toBe("p1");
    fireEvent.click(btn);
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("форматирует таймер мм:сс при > 60c", () => {
    render(
      <ThinkingIndicator activity={tool} elapsedMs={72000} open={false} onToggle={() => {}} panelId="p1" />,
    );
    expect(screen.getByText(/1:12/)).toBeTruthy();
  });
});
