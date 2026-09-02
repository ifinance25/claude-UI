import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/api/client", () => ({
  api: { createSession: vi.fn() },
}));

import { api } from "@/api/client";
import { startNewSession, decideNewChat } from "@/lib/newSession";
import type { Session } from "@/lib/types";

const fakeSession = { session_uuid: "u1", project_path: "/p", project_name: "P" } as Session;

describe("startNewSession", () => {
  beforeEach(() => vi.clearAllMocks());

  it("создаёт сессию с путём/именем проекта и выбирает её", async () => {
    (api.createSession as ReturnType<typeof vi.fn>).mockResolvedValue(fakeSession);
    const onSelect = vi.fn();
    const s = await startNewSession({ path: "/p", name: "P" }, onSelect);
    expect(api.createSession).toHaveBeenCalledWith("/p", "P");
    expect(onSelect).toHaveBeenCalledWith(fakeSession);
    expect(s).toBe(fakeSession);
  });

  it("без проекта → null/null", async () => {
    (api.createSession as ReturnType<typeof vi.fn>).mockResolvedValue(fakeSession);
    await startNewSession(null, vi.fn());
    expect(api.createSession).toHaveBeenCalledWith(null, null);
  });
});

describe("decideNewChat", () => {
  const projects = [{ path: "/p/alpha", name: "alpha" }];

  it("пока список проектов не загружен — ничего не создаём", () => {
    // Именно этот случай уводил чат в data/scratch: пустой projects до
    // загрузки неотличим от «проектов нет», и кнопка создавала сессию с null.
    expect(decideNewChat([], false)).toEqual({ action: "wait" });
    expect(decideNewChat(projects, false)).toEqual({ action: "wait" });
  });

  it("загрузились и пусто — говорим про PROJECTS_DIR, а не молчим", () => {
    expect(decideNewChat([], true)).toEqual({ action: "no-projects" });
  });

  it("загрузились и проект есть — создаём в нём", () => {
    expect(decideNewChat(projects, true)).toEqual({
      action: "create",
      project: { path: "/p/alpha", name: "alpha" },
    });
  });
});
