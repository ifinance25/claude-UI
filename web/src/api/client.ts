// REST client. credentials: 'include' so vels_session cookie is sent.

import type {
  Access,
  AdminUser,
  ApiUser,
  ArtifactsResponse,
  Attachment,
  ConnectionsInfo,
  DocFile,
  FileContent,
  FileEntry,
  FileTreeResponse,
  HistoryMessage,
  ModelInfo,
  Project,
  ProjectAccess,
  ProjectAdmin,
  ProjectMember,
  SearchResponse,
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
  adminListUsers() {
    return req<AdminUser[]>("GET", "/api/admin/users");
  },
  adminCreateUser(username: string, password: string, is_admin: boolean) {
    return req<{ user_id: number }>("POST", "/api/admin/users", {
      username,
      password,
      is_admin,
    });
  },
  adminListAccess(userId: number) {
    return req<ProjectAccess[]>("GET", `/api/admin/users/${userId}/access`);
  },
  adminGrantAccess(
    userId: number,
    project_path: string,
    access_level: "full" | "readonly",
  ) {
    return req<{ ok: boolean }>("POST", `/api/admin/users/${userId}/access`, {
      project_path,
      access_level,
    });
  },
  adminRevokeAccess(userId: number, project_path: string) {
    return req<{ ok: boolean }>(
      "DELETE",
      `/api/admin/users/${userId}/access?project_path=${encodeURIComponent(project_path)}`,
    );
  },
  adminSetActive(userId: number, active: boolean) {
    return req<{ ok: boolean }>(
      "POST",
      `/api/admin/users/${userId}/active?active=${active}`,
    );
  },
  adminResetPassword(userId: number, password: string) {
    return req<{ ok: boolean }>("POST", `/api/admin/users/${userId}/password`, {
      password,
    });
  },
  adminDeleteUser(userId: number) {
    return req<{ ok: boolean }>("DELETE", `/api/admin/users/${userId}`);
  },
  adminSetUserAdmin(userId: number, is_admin: boolean) {
    return req<{ ok: boolean }>("POST", `/api/admin/users/${userId}/admin`, {
      is_admin,
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
  // Тот же файл, но для ПРОСМОТРА в браузере (inline): картинка в <img>, PDF в
  // <iframe>. Бэкенд отдаёт inline только для image/* и application/pdf.
  sessionFileInlineUrl(sessionUuid: string, relPath: string) {
    return apiUrl(
      `/api/sessions/${sessionUuid}/file?path=${encodeURIComponent(relPath)}&inline=1`,
    );
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

  // ── Admin: Projects CRUD ────────────────────────────────────
  adminListProjects() {
    return req<ProjectAdmin[]>("GET", "/api/admin/projects");
  },
  adminCreateProject(abspath: string) {
    return req<ProjectAdmin>("POST", "/api/admin/projects", { abspath });
  },
  adminUpdateProject(projectId: number, abspath: string) {
    return req<ProjectAdmin>("PATCH", `/api/admin/projects/${projectId}`, { abspath });
  },
  adminDeleteProject(projectId: number) {
    return req<{ success: boolean }>("DELETE", `/api/admin/projects/${projectId}`);
  },

  // ── Admin: Accesses CRUD ───────────────────────────────────
  adminListAccesses() {
    return req<Access[]>("GET", "/api/admin/accesses");
  },
  adminCreateAccess(userId: number, projectId: number, accessLevel: string) {
    return req<Access>("POST", "/api/admin/accesses", {
      user_id: userId,
      project_id: projectId,
      access_level: accessLevel,
    });
  },
  adminUpdateAccess(accessId: number, accessLevel: string) {
    return req<Access>("PATCH", `/api/admin/accesses/${accessId}`, {
      access_level: accessLevel,
    });
  },
  adminDeleteAccess(accessId: number) {
    return req<{ success: boolean }>("DELETE", `/api/admin/accesses/${accessId}`);
  },

  // ── Files browser (Фаза 2.2) ──────────────────────────────
  filesTree(projectPath: string, dir = "") {
    return req<FileTreeResponse>(
      "GET",
      `/api/files/tree?project_path=${encodeURIComponent(projectPath)}&dir=${encodeURIComponent(dir)}`,
    );
  },
  fileContent(projectPath: string, rel: string) {
    return req<FileContent>(
      "GET",
      `/api/files/content?project_path=${encodeURIComponent(projectPath)}&rel=${encodeURIComponent(rel)}`,
    );
  },
  saveFile(projectPath: string, rel: string, content: string, expectedMtimeNs: number) {
    return req<{ mtime_ns: number }>(
      "PUT",
      `/api/files/content?project_path=${encodeURIComponent(projectPath)}&rel=${encodeURIComponent(rel)}`,
      { content, expected_mtime_ns: expectedMtimeNs },
    );
  },
  createEntry(projectPath: string, rel: string, kind: "file" | "dir") {
    return req<FileEntry>(
      "POST",
      `/api/files/entry?project_path=${encodeURIComponent(projectPath)}`,
      { rel, kind },
    );
  },
  deleteEntry(projectPath: string, rel: string) {
    return req<{ success: boolean }>(
      "DELETE",
      `/api/files/entry?project_path=${encodeURIComponent(projectPath)}&rel=${encodeURIComponent(rel)}`,
    );
  },
  moveEntry(projectPath: string, src: string, dst: string) {
    return req<{ success: boolean }>(
      "POST",
      `/api/files/move?project_path=${encodeURIComponent(projectPath)}`,
      { src, dst },
    );
  },
  searchFiles(projectPath: string, q: string, mode: "name" | "content") {
    return req<SearchResponse>(
      "GET",
      `/api/files/search?project_path=${encodeURIComponent(projectPath)}&q=${encodeURIComponent(q)}&mode=${mode}`,
    );
  },

  // ── Artifacts (Фаза 2.3) ──────────────────────────────────
  filesArtifacts(projectPath: string) {
    return req<ArtifactsResponse>(
      "GET",
      `/api/files/artifacts?project_path=${encodeURIComponent(projectPath)}`,
    );
  },
  dismissArtifact(projectPath: string, rel: string) {
    return req<{ success: boolean }>(
      "POST",
      `/api/files/artifacts/dismiss?project_path=${encodeURIComponent(projectPath)}`,
      { rel },
    );
  },
  undismissArtifact(projectPath: string, rel: string) {
    return req<{ success: boolean }>(
      "DELETE",
      `/api/files/artifacts/dismiss?project_path=${encodeURIComponent(projectPath)}&rel=${encodeURIComponent(rel)}`,
    );
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

  // ── Project members (self-service sharing) ────────────────
  // Управление участниками проекта пользователем с full-доступом (не админка).
  // Ключ — числовой project_id. Не-админ управляет только своими грантами.
  listMembers(projectId: number) {
    return req<ProjectMember[]>("GET", `/api/projects/${projectId}/members`);
  },
  addMember(
    projectId: number,
    identifier: string,
    accessLevel: "full" | "readonly",
  ) {
    return req<{ user_id: number; access_level: string }>(
      "POST",
      `/api/projects/${projectId}/members`,
      { identifier, access_level: accessLevel },
    );
  },
  updateMember(
    projectId: number,
    userId: number,
    accessLevel: "full" | "readonly",
  ) {
    return req<{ user_id: number; access_level: string }>(
      "PATCH",
      `/api/projects/${projectId}/members/${userId}`,
      { access_level: accessLevel },
    );
  },
  removeMember(projectId: number, userId: number) {
    return req<{ removed: number }>(
      "DELETE",
      `/api/projects/${projectId}/members/${userId}`,
    );
  },
};
