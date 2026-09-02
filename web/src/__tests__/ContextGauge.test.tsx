import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import ContextGauge from "@/components/ContextGauge";

const base = {
  contextTokens: 84_000,
  window: 1_000_000,
  onNewChat: () => {},
  onCompact: () => {},
  autoOpenTrigger: 0,
};

describe("ContextGauge", () => {
  it("null при нулевом контексте", () => {
    const { container } = render(<ContextGauge {...base} pct={0} contextTokens={0} />);
    expect(container.firstChild).toBeNull();
  });

  it("пилюля показывает % и зону", () => {
    render(<ContextGauge {...base} pct={42} />);
    const btn = screen.getByRole("button", { name: /контекст/i });
    expect(btn.getAttribute("data-zone")).toBe("green");
    expect(screen.getByText(/42% контекст/)).toBeTruthy();
  });

  it("клик открывает поповер с токенами/окном", () => {
    render(<ContextGauge {...base} pct={42} />);
    fireEvent.click(screen.getByRole("button", { name: /контекст/i }));
    expect(screen.getByText(/84 000|84,000|84000/)).toBeTruthy();
    expect(screen.getByText(/1[\s ,]?000[\s ,]?000/)).toBeTruthy();
  });

  it("кнопки действий только в красной зоне", () => {
    const { rerender } = render(<ContextGauge {...base} pct={72} />);
    fireEvent.click(screen.getByRole("button", { name: /контекст/i }));
    expect(screen.queryByRole("button", { name: "Новый чат" })).toBeNull();
    // Поповер остаётся открытым при rerender — второй клик по пилюле НЕ нужен
    // (иначе toggle закрыл бы его). Красная зона → появляются кнопки действий.
    rerender(<ContextGauge {...base} pct={91} contextTokens={182_000} />);
    expect(screen.getByRole("button", { name: "Новый чат" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Сжать" })).toBeTruthy();
  });

  it("кнопки зовут колбэки", () => {
    const onNewChat = vi.fn();
    const onCompact = vi.fn();
    render(
      <ContextGauge {...base} pct={91} contextTokens={182_000} onNewChat={onNewChat} onCompact={onCompact} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /контекст/i }));
    fireEvent.click(screen.getByRole("button", { name: "Новый чат" }));
    expect(onNewChat).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: /контекст/i }));
    fireEvent.click(screen.getByRole("button", { name: "Сжать" }));
    expect(onCompact).toHaveBeenCalledOnce();
  });

  it("autoOpenTrigger раскрывает поповер без клика", () => {
    const { rerender } = render(<ContextGauge {...base} pct={91} contextTokens={182_000} autoOpenTrigger={0} />);
    expect(screen.queryByText(/до заполнения/)).toBeNull();
    rerender(<ContextGauge {...base} pct={91} contextTokens={182_000} autoOpenTrigger={1} />);
    expect(screen.getByText(/до заполнения/)).toBeTruthy();
  });

  it("закрывается по клику вне и по Escape", () => {
    render(<ContextGauge {...base} pct={91} contextTokens={182_000} />);
    fireEvent.click(screen.getByRole("button", { name: /контекст/i }));
    expect(screen.getByText(/до заполнения/)).toBeTruthy();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByText(/до заполнения/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /контекст/i }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByText(/до заполнения/)).toBeNull();
  });
});
