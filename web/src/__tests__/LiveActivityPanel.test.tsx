import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { LiveActivityPanel } from "@/components/LiveActivityPanel";

describe("LiveActivityPanel", () => {
  it("показывает живой поток мыслей", () => {
    render(<LiveActivityPanel id="p1" thinking="Надо понять, как устроен стриминг." />);
    expect(screen.getByText(/как устроен стриминг/)).toBeTruthy();
  });

  it("пустой поток → «Подключаюсь…»", () => {
    render(<LiveActivityPanel id="p1" thinking="" />);
    expect(screen.getByText(/Подключаюсь/)).toBeTruthy();
  });

  it("имеет id для aria-controls", () => {
    const { container } = render(<LiveActivityPanel id="p1" thinking="x" />);
    expect(container.querySelector("#p1")).toBeTruthy();
  });

  it("непрозрачная подложка — не просвечивает ленту сообщений", () => {
    const { container } = render(<LiveActivityPanel id="p1" thinking="x" />);
    const panel = container.querySelector("#p1") as HTMLElement;
    expect(panel.className).toContain("bg-[var(--bg-sidebar)]");
    expect(panel.className).not.toContain("/70");
  });
});
