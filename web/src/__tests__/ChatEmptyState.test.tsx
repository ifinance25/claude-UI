import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ChatEmptyState from "@/components/ChatEmptyState";

// На свежей установке PROJECTS_DIR пуст, и сессия молча уходит во временную
// папку data/scratch. Подсказка «нет проектов» жила в секции проектов сайдбара,
// а её в light вырезали — человек оставался без единого намёка, почему Claude
// не видит его файлов.
describe("ChatEmptyState", () => {
  it("без проекта объясняет, что работа идёт во временной папке", () => {
    render(<ChatEmptyState projectName="Без проекта" projectMissing />);
    expect(screen.getByText(/Проект не подключён/)).toBeTruthy();
    expect(screen.getByText(/data\/scratch/)).toBeTruthy();
    expect(screen.getByText(/PROJECTS_DIR/)).toBeTruthy();
  });

  it("с проектом подсказку не показывает", () => {
    render(<ChatEmptyState projectName="alpha" />);
    expect(screen.queryByText(/Проект не подключён/)).toBeNull();
    expect(screen.getByText("alpha")).toBeTruthy();
  });
});
