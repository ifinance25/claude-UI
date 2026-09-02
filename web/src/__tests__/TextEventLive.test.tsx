import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { TextEvent } from "@/components/events/TextEvent";

const OPEN = "\x01THINKING\x02";
const CLOSE = "\x02THINKING\x01";

describe("TextEvent — живой vs исторический блок «Размышления»", () => {
  const raw = `${OPEN}рассуждаю${CLOSE}\n\nВот ответ.`;

  it("ЖИВОЙ бабл (live=true) при reasoningOn НЕ показывает инлайн-блок", () => {
    render(<TextEvent content={raw} reasoningOn={true} live={true} />);
    // Главное: пока бабл живой, инлайнового блока «Размышления» нет
    // (мыслями владеет живая панель — иначе при verbose ≥ 2 они бы двоились).
    expect(screen.queryByText("Размышления")).toBeNull();
    // ...и сама мысль «рассуждаю» в баблe тоже не отображается.
    // (Видимый текст печатается «машинкой» через requestAnimationFrame, которого
    //  в JSDOM нет — поэтому его наличие тут НЕ проверяем, это timing-sensitive.)
    expect(screen.queryByText(/рассуждаю/)).toBeNull();
  });

  it("ЗАВЕРШЁННЫЙ бабл (live=false) при reasoningOn показывает инлайн-блок", () => {
    render(<TextEvent content={raw} reasoningOn={true} live={false} />);
    expect(screen.getByText("Размышления")).toBeTruthy();
  });
});
