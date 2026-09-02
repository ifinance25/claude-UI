// Mirror of backend Pydantic models — keep in sync with src/web/models.py
// and src/event_bus/events.py.AgentStreamingUpdate.kind

export type StreamingUpdateKind =
  | "init"
  | "log"
  | "tool_use"
  | "subagent_start"
  | "subagent_log"
  | "subagent_finish"
  | "text"
  | "thinking"
  | "usage";

export type MessageSource = "telegram" | "web" | "webhook" | "cron";

export type WsMessage =
  | {
      type: "user_message";
      request_id: string;
      content: string;
      source?: MessageSource;
    }
  | { type: "agent_started"; request_id: string }
  | {
      type: "streaming_update";
      request_id: string;
      kind: StreamingUpdateKind;
      content: string;
      metadata: Record<string, unknown>;
    }
  | {
      type: "finished";
      request_id: string;
      session_id: string | null;
      response_text: string;
      usage: Record<string, unknown> | null;
      error: string | null;
      elapsed_ms?: number;
    }
  | { type: "error"; error: string }
  | { type: "gap"; session_uuid?: string; reason?: string };

export interface Project {
  name: string;
  path: string;
}

export interface ProjectAdmin {
  id: number;
  abspath: string;
}

export interface Access {
  id: number;
  user_id: number;
  username: string | null;
  project_id: number | null;
  project_path: string | null;
  access_level: string;
}

export interface Session {
  topic_id: number;
  session_uuid: string;
  project_path: string;
  project_name: string;
  // Числовой id проекта (projects.id); null для сессий «без проекта».
  // Ключ для /api/projects/{project_id}/members (управление участниками).
  project_id: number | null;
  session_id: string | null;
  status: string;
  message_count: number;
  total_cost_usd: number;
  created_at: string;
  last_activity: string;
  notes: string | null;
  is_running?: boolean;
  // Может ли текущий пользователь управлять участниками проекта этой сессии —
  // вычислено бэком ТЕМ ЖЕ предикатом, что и серверный /members
  // (require_project_manage): admin ИЛИ явный full-грант именно на этот
  // проект. Гейтит показ кнопки «Участники» (см. Chat.tsx) — без клиентской
  // эвристики поверх resolve_project_access, которая рассинхронилась с
  // реальным правом на управление (L-8).
  can_manage_members?: boolean;
}

export interface HistoryMessage {
  event_id: number;
  topic_id: number;
  request_id: string | null;
  type: string;
  kind: string | null;
  content: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
}

// Публичная конфигурация страницы входа (/api/auth/config): её читают до
// авторизации, когда user'а ещё нет. В light Telegram необязателен, и без бота
// страница входа не должна о нём упоминать.
export interface AuthConfig {
  telegram_enabled: boolean;
  telegram_bot_username: string;
}

export interface ApiUser {
  id: number;
  username: string;
  is_admin?: boolean;
  telegram_bot_username?: string;
  can_continue_in_telegram?: boolean;
}

export interface AdminUser {
  user_id: number;
  username: string;
  is_admin: number;
  is_active: number;
  // Per-user Anthropic API-key indicator (SP2). Never carries the key itself —
  // only whether one is set, its status and the last-4 fragment.
  has_key?: boolean;
  key_status?: "active" | "unverified" | "needs_reentry" | null;
  key_last4?: string | null;
}

export interface ProjectAccess {
  project_path: string;
  access_level: string;
}

// Участник проекта (self-service шаринг). `manageable` — может ли текущий
// пользователь менять/убирать этот грант (свой грант или он админ; не про
// себя и не про админский грант).
export interface ProjectMember {
  user_id: number;
  username: string | null;
  access_level: "full" | "readonly";
  manageable: boolean;
}

export interface ModelInfo {
  current: string;
  permission_mode: string;
  known: { id: string; label: string; hint: string }[];
}

export interface SlashCommand {
  cmd: string;
  label: string;
  target: "bot" | "claude";
  kind?: "command" | "skill";
}

export interface Attachment {
  source_path: string;
  file_name: string;
  mime_type: string;
  kind: "image" | "text";
  size_bytes: number;
}

export interface DocFile {
  rel: string;
  size_bytes: number;
}

// ── Files browser (Фаза 2.2) ──────────────────────────────
export type FileEntryType = "file" | "dir";

export interface FileEntry {
  name: string;
  rel: string;
  type: FileEntryType;
  size_bytes: number | null;
}

export interface FileTreeResponse {
  access_level: "full" | "readonly";
  truncated: boolean;
  entries: FileEntry[];
}

export interface FileContent {
  rel: string;
  content: string;
  size_bytes: number;
  mtime_ns: number;
  binary: boolean;
  too_large: boolean;
}

export interface SearchHit {
  rel: string;
  line: number | null;
  preview: string | null;
}

export interface SearchResponse {
  truncated: boolean;
  hits: SearchHit[];
}

// ── Artifacts (Фаза 2.3) ──────────────────────────────────
export interface Artifact {
  rel: string;
  name: string;
  display: string;
  action: "created" | "edited";
  edits: number;
  last_ts: string;
  exists: boolean;
  /** Папка (скачивается zip'ом). */
  is_dir?: boolean;
  size_bytes: number | null;
  dismissed: boolean;
}

export interface ArtifactsResponse {
  access_level: "full" | "readonly";
  artifacts: Artifact[];
}

// ── Connect Services (Фаза 3) ─────────────────────────────
export interface ConnectionService {
  id: string;
  name: string;
  icon: string;
  description: string;
  how_to_url: string;
  how_to_steps: string[];
  secret_label: string;
  connected: boolean;
}

export interface ConnectionsInfo {
  enabled: boolean;
  services: ConnectionService[];
}
