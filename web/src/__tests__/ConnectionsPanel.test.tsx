import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import ConnectionsPanel from "@/components/ConnectionsPanel";
import { api } from "@/api/client";

beforeEach(() => {
  vi.spyOn(api, "listConnections").mockResolvedValue({
    enabled: true,
    services: [
      { id: "github", name: "GitHub", icon: "🐙", description: "d", how_to_url: "u", how_to_steps: ["s"], secret_label: "PAT", connected: false },
      { id: "notion", name: "Notion", icon: "📝", description: "d", how_to_url: "u", how_to_steps: ["s"], secret_label: "Token", connected: true },
    ],
  });
});

describe("ConnectionsPanel", () => {
  it("lists services with connect/disconnect by status", async () => {
    render(<ConnectionsPanel />);
    await waitFor(() => screen.getByText("GitHub"));
    expect(screen.getByText("Notion")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Отключить/i })).toBeTruthy();
  });

  it("shows disabled notice when enabled=false", async () => {
    vi.spyOn(api, "listConnections").mockResolvedValue({ enabled: false, services: [] });
    render(<ConnectionsPanel />);
    await waitFor(() => screen.getByText(/выключены|недоступн/i));
  });
});
