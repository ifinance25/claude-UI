import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { UsageEvent } from "@/components/events/UsageEvent";

// Подвал хода показывает только контекст и время ответа. Счётчики токенов
// (всего / in-out / кэш) и стоимость в долларах убраны по просьбе пользователя.
const usage = {
  input_tokens: 2,
  output_tokens: 25,
  cache_read_input_tokens: 36714,
  cache_creation_input_tokens: 0,
  cost_usd: 0.3678,
  __elapsed_ms: 12400,
};

describe("подвал хода", () => {
  it("не показывает токены, кэш и стоимость", () => {
    const { container } = render(<UsageEvent usage={usage} modelId="claude-opus-5" />);
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/tokens/i);
    expect(text).not.toMatch(/in\/out/i);
    expect(text).not.toMatch(/cache/i);
    expect(text).not.toContain("$");
    expect(text).not.toContain("36 741");
    expect(text).not.toContain("0.3678");
  });

  it("показывает контекст и время ответа", () => {
    render(<UsageEvent usage={usage} modelId="claude-opus-5" />);
    expect(screen.getByText(/контекст \d+%/)).toBeTruthy();
    expect(screen.getByText("12.4с")).toBeTruthy();
  });

  it("без данных не рисует пустую полосу между кусками ответа", () => {
    const { container } = render(<UsageEvent usage={{}} />);
    expect(container.firstChild).toBeNull();
  });
});
