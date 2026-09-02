import { describe, it, expect } from "vitest";
import { pendingRequestId } from "@/lib/pendingTurn";
import type { HistoryMessage } from "@/lib/types";

const row = (
  type: string,
  request_id: string | null,
  kind: string | null = null,
): HistoryMessage =>
  ({ event_id: 1, type, kind, request_id, content: "", metadata: null }) as unknown as HistoryMessage;

describe("незавершённый ход при возврате в диалог", () => {
  it("ушли, пока Claude думал — ход считается незавершённым", () => {
    // Именно это видел пользователь: вопрос есть, текста ещё нет, и без
    // индикатора чат выглядел сброшенным.
    const history = [
      row("user_message", "r1"),
      row("agent_started", "r1"),
      row("streaming_update", "r1", "init"),
    ];
    expect(pendingRequestId(history)).toBe("r1");
  });

  it("ход дописан — индикатор не нужен", () => {
    const history = [
      row("user_message", "r1"),
      row("streaming_update", "r1", "text"),
      row("finished", "r1"),
    ];
    expect(pendingRequestId(history)).toBeNull();
  });

  it("незавершён только последний ход, прежние закрыты", () => {
    const history = [
      row("user_message", "r1"),
      row("finished", "r1"),
      row("user_message", "r2"),
      row("streaming_update", "r2", "text"),
    ];
    expect(pendingRequestId(history)).toBe("r2");
  });

  it("пустая история и строки без request_id ничего не подвешивают", () => {
    expect(pendingRequestId([])).toBeNull();
    expect(pendingRequestId([row("streaming_update", null, "log")])).toBeNull();
  });
});
