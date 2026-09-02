// REST client. credentials: 'include' so vels_session cookie is sent.

import type {
  ApiUser,
  AuthConfig,
  Attachment,
  ConnectionsInfo,
  DocFile,
  HistoryMessage,
  ModelInfo,
  Project,
  Session,
  SlashCommand,
} from "@/lib/types";
import { apiUrl } from "@/api/base";

const jsonHeaders = { "Content-Type": "application/json" };

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

// Глобальный сигнал «сессия истекла». AuthProvider слушает его и сбрасывает
// user → ProtectedRoute редиректит на /login. Без этого протухшая cookie
// посреди работы оставляла приложение висеть на баннере ошибки (поллинг
// Sidebar бил 401 каждые 4с) до ручной перезагрузки.
export const AUTH_UNAUTHORIZED_EVENT = "auth:unauthorized";

function notifyIfUnauthorized(status: number, path: string): void {
  // Явные auth-флоу (/api/auth/*) исключаем: там 401 = неверные креды,
  // обрабатывается локально на странице входа, а не глобальным разлогином.
  if (
    status === 401 &&
    typeof window !== "undefined" &&
    !path.startsWith("/api/auth/")
  ) {
    window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(apiUrl(path), {
    method,
    headers: jsonHeaders,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    const text = await resp.text();
    notifyIfUnauthorized(resp.status, path);
    throw new ApiError(resp.status, `${resp.status} ${resp.statusText}: ${text}`);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export const api = {
  // Что показывать на странице входа: подключён ли Telegram-бот. Без
  // авторизации — на /login пользователя ещё нет.
  authConfig() {
    return req<AuthConfig>("GET", "/api/auth/config");
  },
  telegramLogin(payload: Record<string, unknown>) {
    return req<{ ok: boolean; user: ApiUser }>("POST", "/api/auth/telegram", payload);
  },
  devLogin(token: string) {
    return req<{ ok: boolean; user: ApiUser }>("POST", "/api/auth/dev-login", { token });
  },
  magicLink(token: string) {
    return req<{ ok: boolean; user: ApiUser }>("POST", "/api/auth/magic-link", { token });
  },
  login(username: string, password: string) {
    return req<{ ok: boolean; user: ApiUser }>("POST", "/api/auth/login", {
      username,
      password,
    });
  },
  logout() {
    return req<void>("POST", "/api/auth/logout");
  },
  me() {
    return req<ApiUser>("GET", "/api/me");
  },
  listProjects() {
    return req<Project[]>("GET", "/api/projects");
  },
  listSessions(q: string = "") {
    const path = q ? `/api/sessions?q=${encodeURIComponent(q)}` : "/api/sessions";
    return req<Session[]>("GET", path);
  },
  createSession(project_path: string | null, project_name: string | null) {
    return req<Session>("POST", "/api/sessions", { project_path, project_name });
  },
  deleteSession(session_uuid: string) {
    return req<void>("DELETE", `/api/sessions/${session_uuid}`);
  },
  patchSessionNotes(session_uuid: string, notes: string) {
    return req<Session>("PATCH", `/api/sessions/${session_uuid}`, { notes });
  },
  getMessages(session_uuid: string, since = 0, limit = 1000) {
    return req<HistoryMessage[]>(
      "GET",
      `/api/sessions/${session_uuid}/messages?since=${since}&limit=${limit}`
    );
  },
  getSettings() {
    return req<{ verbose_level: 0 | 1 | 2 | 3 }>("GET", "/api/settings");
  },
  patchSettings(verbose_level: 0 | 1 | 2 | 3) {
    return req<{ verbose_level: 0 | 1 | 2 | 3 }>(
      "PATCH",
      "/api/settings",
      { verbose_level }
    );
  },
  getModel() {
    return req<ModelInfo>("GET", "/api/model");
  },
  patchModel(model: string) {
    return req<ModelInfo>("PATCH", "/api/model", { model });
  },
  getSlashCommands(projectPath?: string) {
    const path = projectPath
      ? `/api/slash-commands?project_path=${encodeURIComponent(projectPath)}`
      : "/api/slash-commands";
    return req<SlashCommand[]>("GET", path);
  },
  getGuide() {
    return req<{ content: string }>("GET", "/api/docs/guide");
  },
  listDocs(projectPath: string) {
    return req<DocFile[]>(
      "GET",
      `/api/docs?project_path=${encodeURIComponent(projectPath)}`,
    );
  },
  getDocContent(projectPath: string, rel: string) {
    return req<{ rel: string; content: string }>(
      "GET",
      `/api/docs/content?project_path=${encodeURIComponent(
        projectPath,
      )}&rel=${encodeURIComponent(rel)}`,
    );
  },
  sessionFileUrl(sessionUuid: string, relPath: string) {
    return apiUrl(`/api/sessions/${sessionUuid}/file?path=${encodeURIComponent(relPath)}`);
  },
  async uploadFile(sessionUuid: string, file: File): Promise<Attachment> {
    // Multipart нельзя слать через стандартный JSON `req` —
    // используем отдельный fetch с FormData.
    const form = new FormData();
    form.append("session_uuid", sessionUuid);
    form.append("file", file);
    const resp = await fetch(apiUrl("/api/uploads"), {
      method: "POST",
      credentials: "include",
      body: form,
    });
    if (!resp.ok) {
      const text = await resp.text();
      notifyIfUnauthorized(resp.status, "/api/uploads");
      throw new ApiError(resp.status, `${resp.status} ${resp.statusText}: ${text}`);
    }
    return (await resp.json()) as Attachment;
  },

  // ── Connect Services (Фаза 3) ─────────────────────────────
  listConnections() {
    return req<ConnectionsInfo>("GET", "/api/connections");
  },
  connectService(service_id: string, secret: string) {
    return req<{ ok: boolean }>("POST", "/api/connections", { service_id, secret });
  },
  disconnectService(service_id: string) {
    return req<{ ok: boolean }>("DELETE", `/api/connections/${service_id}`);
  },

  // ── Per-user Anthropic API key (SP2) ──────────────────────
  // GET returns only non-secret metadata (status/last4/timestamps), never the
  // key itself. `privileged` echoes whether this caller may fall back to the
  // service subscription (owner/admin) — the panel uses it for helper text.
  getApiKey() {
    return req<{
      status: string | null;
      last4: string | null;
      created_at?: string | null;
      updated_at?: string | null;
      privileged?: boolean;
    }>("GET", "/api/apikey");
  },
  putApiKey(apiKey: string) {
    return req<{ status: string; message: string; warning?: string }>(
      "PUT",
      "/api/apikey",
      { api_key: apiKey },
    );
  },
  deleteApiKey() {
    return req<{ message: string }>("DELETE", "/api/apikey");
  },

};
