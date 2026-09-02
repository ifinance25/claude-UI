import { describe, it, expect } from "vitest";
import { mergeTargetIndex, type MergeBubble } from "@/lib/bubbleMerge";

const text = (requestId: string): MergeBubble => ({ kind: "text", requestId });

describe("склейка кусков ответа", () => {
  it("дописывает в последний text-бабл того же хода", () => {
    expect(mergeTargetIndex([text("r1")], "r1")).toBe(0);
  });

  it("служебное событие между кусками НЕ рвёт ответ (регресс «М» / «ы в …»)", () => {
    // Именно это ломало вёрстку: usage-плашка прилетала посреди стрима, и
    // остаток текста уходил в новый markdown-блок, разрывая слово.
    for (const kind of ["usage", "log", "init", "subagent_log"]) {
      const list: MergeBubble[] = [text("r1"), { kind, requestId: "r1" }];
      expect(mergeTargetIndex(list, "r1")).toBe(0);
    }
  });

  it("перешагивает несколько служебных событий подряд", () => {
    const list: MergeBubble[] = [
      text("r1"),
      { kind: "log", requestId: "r1" },
      { kind: "usage", requestId: null },
    ];
    expect(mergeTargetIndex(list, "r1")).toBe(0);
  });

  it("вызов инструмента остаётся границей: текст до и после — разные блоки", () => {
    const list: MergeBubble[] = [text("r1"), { kind: "tool_use", requestId: "r1" }];
    expect(mergeTargetIndex(list, "r1")).toBe(-1);
  });

  it("чужой ход не склеивается", () => {
    expect(mergeTargetIndex([text("r1")], "r2")).toBe(-1);
  });

  it("пустая лента и ход без текста требуют нового бабла", () => {
    expect(mergeTargetIndex([], "r1")).toBe(-1);
    expect(mergeTargetIndex([{ kind: "user_message", requestId: "r1" }], "r1")).toBe(-1);
  });
});
