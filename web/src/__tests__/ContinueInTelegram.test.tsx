import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ContinueInTelegram } from "@/components/ContinueInTelegram";

describe("ContinueInTelegram", () => {
  it("рендерит ссылку deeplink когда имя бота задано", () => {
    render(<ContinueInTelegram botUsername="velsbot" sessionUuid="abc" />);
    const link = screen.getByRole("link", { name: /Telegram/i });
    expect(link).toHaveAttribute(
      "href",
      "https://t.me/velsbot?start=continue_abc",
    );
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("ничего не рендерит без имени бота", () => {
    const { container } = render(
      <ContinueInTelegram botUsername="" sessionUuid="abc" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("скрыта у локальных юзеров (canContinue=false) даже при наличии бота", () => {
    const { container } = render(
      <ContinueInTelegram
        botUsername="velsbot"
        sessionUuid="abc"
        canContinue={false}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
