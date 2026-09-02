import { describe, it, expect, vi, afterEach } from "vitest";
import { api } from "@/api/client";

afterEach(() => vi.restoreAllMocks());

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: status < 400, status, statusText: "",
    text: async () => JSON.stringify(body), json: async () => body,
  })) as unknown as typeof fetch);
}

describe("connections api", () => {
  it("listConnections GETs /api/connections", async () => {
    mockFetch(200, { enabled: true, services: [] });
    const r = await api.listConnections();
    expect(r.enabled).toBe(true);
    expect((fetch as any).mock.calls[0][0]).toContain("/api/connections");
  });
  it("connectService POSTs service_id+secret", async () => {
    mockFetch(200, { ok: true });
    await api.connectService("github", "PAT");
    const [, opts] = (fetch as any).mock.calls[0];
    expect(opts.method).toBe("POST");
    expect(opts.body).toContain("github");
  });
  it("disconnectService DELETEs by id", async () => {
    mockFetch(200, { ok: true });
    await api.disconnectService("github");
    const [url, opts] = (fetch as any).mock.calls[0];
    expect(opts.method).toBe("DELETE");
    expect(url).toContain("/api/connections/github");
  });
});
