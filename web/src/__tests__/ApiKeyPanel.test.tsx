import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ApiKeyPanel from "@/components/ApiKeyPanel";
import { api, ApiError } from "@/api/client";

const VALID_KEY = "sk-ant-" + "a".repeat(48);

beforeEach(() => {
  vi.restoreAllMocks();
  // Default: no key stored.
  vi.spyOn(api, "getApiKey").mockResolvedValue({
    status: null,
    last4: null,
    created_at: null,
    updated_at: null,
    privileged: false,
  });
});

describe("ApiKeyPanel", () => {
  it("loads status on mount and shows 'не задан' when empty", async () => {
    render(<ApiKeyPanel privileged={false} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());
    expect(await screen.findByText("не задан")).toBeInTheDocument();
  });

  it("renders masked key + status badge when a key exists", async () => {
    vi.spyOn(api, "getApiKey").mockResolvedValue({
      status: "active",
      last4: "AB12",
      privileged: false,
    });
    render(<ApiKeyPanel privileged={false} />);
    expect(await screen.findByText("sk-…AB12")).toBeInTheDocument();
    expect(screen.getByText("активен")).toBeInTheDocument();
  });

  it("renders 'нужен повторный ввод' for a needs_reentry key (parity with admin)", async () => {
    vi.spyOn(api, "getApiKey").mockResolvedValue({
      status: "needs_reentry",
      last4: "AB12",
      privileged: false,
    });
    render(<ApiKeyPanel privileged={false} />);
    expect(await screen.findByText("sk-…AB12")).toBeInTheDocument();
    expect(screen.getByText("нужен повторный ввод")).toBeInTheDocument();
    expect(screen.queryByText("не проверен")).not.toBeInTheDocument();
  });

  it("shows unprivileged helper text", async () => {
    render(<ApiKeyPanel privileged={false} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());
    expect(
      screen.getByText(/Без ключа отправка сообщений недоступна/i),
    ).toBeInTheDocument();
  });

  it("shows privileged (admin) helper text", async () => {
    vi.spyOn(api, "getApiKey").mockResolvedValue({
      status: null,
      last4: null,
      created_at: null,
      updated_at: null,
      privileged: true,
    });
    render(<ApiKeyPanel privileged={true} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());
    expect(
      screen.getByText(/по умолчанию подписка сервиса/i),
    ).toBeInTheDocument();
  });

  it("prefers the backend 'privileged' flag over the prop (whitelist non-admin)", async () => {
    // JWT is_admin=false (prop) but the backend honors the whitelist → owner
    // fallback available, so the scarier 'Без ключа…' text must NOT appear.
    vi.spyOn(api, "getApiKey").mockResolvedValue({
      status: null,
      last4: null,
      created_at: null,
      updated_at: null,
      privileged: true,
    });
    render(<ApiKeyPanel privileged={false} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());
    expect(
      await screen.findByText(/по умолчанию подписка сервиса/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Без ключа отправка сообщений недоступна/i),
    ).not.toBeInTheDocument();
  });

  it("rejects a malformed key client-side without calling the API", async () => {
    const put = vi.spyOn(api, "putApiKey");
    render(<ApiKeyPanel privileged={false} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("sk-ant-…"), {
      target: { value: "not-a-key" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(
      await screen.findByText(/должен начинаться с sk-ant-/i),
    ).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
  });

  it("saves a valid key and shows success", async () => {
    const put = vi
      .spyOn(api, "putApiKey")
      .mockResolvedValue({ status: "active", message: "API key saved" });
    render(<ApiKeyPanel privileged={false} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("sk-ant-…"), {
      target: { value: VALID_KEY },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    await waitFor(() => expect(put).toHaveBeenCalledWith(VALID_KEY));
    expect(await screen.findByText("API key saved")).toBeInTheDocument();
  });

  it("surfaces a server validation error (probe 401 → 422 detail)", async () => {
    vi.spyOn(api, "putApiKey").mockRejectedValue(
      new ApiError(
        422,
        '422 Unprocessable Entity: {"detail":"API key failed validation (401 Unauthorized)"}',
      ),
    );
    render(<ApiKeyPanel privileged={false} />);
    await waitFor(() => expect(api.getApiKey).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("sk-ant-…"), {
      target: { value: VALID_KEY },
    });
    fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));

    expect(
      await screen.findByText(/failed validation \(401 Unauthorized\)/i),
    ).toBeInTheDocument();
  });

  it("deletes an existing key", async () => {
    vi.spyOn(api, "getApiKey").mockResolvedValue({
      status: "active",
      last4: "AB12",
      privileged: false,
    });
    const del = vi
      .spyOn(api, "deleteApiKey")
      .mockResolvedValue({ message: "API key deleted" });
    render(<ApiKeyPanel privileged={false} />);
    await screen.findByText("sk-…AB12");

    fireEvent.click(screen.getByRole("button", { name: "Удалить" }));

    await waitFor(() => expect(del).toHaveBeenCalled());
    expect(await screen.findByText("API key deleted")).toBeInTheDocument();
  });

  it("shows a disabled notice when the feature is dormant (501)", async () => {
    vi.spyOn(api, "getApiKey").mockRejectedValue(
      new ApiError(501, "501 Not Implemented: api key store not configured"),
    );
    render(<ApiKeyPanel privileged={false} />);
    expect(
      await screen.findByText(/выключено на сервере/i),
    ).toBeInTheDocument();
  });
});
