import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import type { AdminUser, ProjectAdmin, ProjectAccess } from "@/lib/types";

export default function UserTab() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [projects, setProjects] = useState<ProjectAdmin[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ username: "", password: "", is_admin: false });
  const [showPw, setShowPw] = useState(false);

  const reload = async () => {
    try {
      setUsers(await api.adminListUsers());
      setProjects(await api.adminListProjects());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  const createUser = async () => {
    try {
      await api.adminCreateUser(form.username, form.password, form.is_admin);
      setForm({ username: "", password: "", is_admin: false });
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      {error && (
        <div className="mb-3 rounded-xl bg-red-900/30 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="mb-6 flex flex-wrap items-end gap-2 rounded-2xl border border-[var(--border-subtle)] p-4">
        <input
          placeholder="логин"
          value={form.username}
          onChange={(e) => setForm({ ...form, username: e.target.value })}
          className="rounded-lg bg-[var(--bg-input)] px-3 py-2"
        />
        <div className="relative">
          <input
            placeholder="пароль (мин. 12)"
            type={showPw ? "text" : "password"}
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            className="rounded-lg bg-[var(--bg-input)] px-3 py-2 pr-16"
          />
          <button
            type="button"
            onClick={() => setShowPw((v) => !v)}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-[var(--fg-muted)] hover:text-[var(--fg-primary)]"
          >
            {showPw ? "скрыть" : "показ"}
          </button>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.is_admin}
            onChange={(e) => setForm({ ...form, is_admin: e.target.checked })}
          />
          админ
        </label>
        <button
          onClick={() => void createUser()}
          className="rounded-lg bg-[var(--accent)] px-4 py-2 text-[var(--bg-canvas)]"
        >
          Создать
        </button>
      </div>

      <div className="space-y-2">
        {users.map((u) => (
          <UserRow
            key={u.user_id}
            user={u}
            projects={projects}
            currentUserId={currentUser?.id}
            onError={setError}
            onChanged={reload}
          />
        ))}
      </div>
    </div>
  );
}

// Небольшой значок статуса per-user Anthropic ключа (SP2). Только индикатор —
// сам ключ на фронт не приходит, лишь has_key/status/last4.
function KeyBadge({ user }: { user: AdminUser }) {
  if (!user.has_key) {
    return <span className="text-xs text-[var(--fg-muted)]">ключ: не задан</span>;
  }
  const last4 = user.key_last4 ? `••••${user.key_last4}` : "";
  if (user.key_status === "active") {
    return (
      <span className="text-xs text-emerald-400">ключ: активен {last4}</span>
    );
  }
  if (user.key_status === "needs_reentry") {
    return (
      <span className="text-xs text-red-400">ключ: нужен повторный ввод</span>
    );
  }
  // unverified / прочее
  return <span className="text-xs text-amber-400">ключ: не проверен {last4}</span>;
}

function UserRow({
  user,
  projects,
  currentUserId,
  onError,
  onChanged,
}: {
  user: AdminUser;
  projects: ProjectAdmin[];
  currentUserId?: number;
  onError: (m: string) => void;
  onChanged: () => void;
}) {
  const [access, setAccess] = useState<ProjectAccess[]>([]);
  const [sel, setSel] = useState<{ project_path: string; access_level: "full" | "readonly" }>({
    project_path: projects[0]?.abspath ?? "",
    access_level: "full",
  });
  const [status, setStatus] = useState<string>("");
  // Инлайн-действия (не window.prompt/confirm — браузер может их блокировать).
  const [action, setAction] = useState<null | "pw" | "delete">(null);
  const [pwValue, setPwValue] = useState("");
  const [rowMsg, setRowMsg] = useState("");

  useEffect(() => {
    api
      .adminListAccess(user.user_id)
      .then(setAccess)
      .catch((e) => onError((e as Error).message));
  }, [user.user_id]);

  const effectivePath = sel.project_path || projects[0]?.abspath || "";

  const grant = async () => {
    if (!effectivePath) {
      onError("Нет доступных проектов для выдачи.");
      return;
    }
    setStatus("…");
    try {
      await api.adminGrantAccess(user.user_id, effectivePath, sel.access_level);
      setAccess(await api.adminListAccess(user.user_id));
      setStatus(`✓ выдан (${sel.access_level === "full" ? "полный" : "только чтение"})`);
    } catch (e) {
      setStatus("");
      onError((e as Error).message);
    }
  };

  const revoke = async (project_path: string) => {
    try {
      await api.adminRevokeAccess(user.user_id, project_path);
      setAccess(await api.adminListAccess(user.user_id));
      setStatus("");
    } catch (e) {
      onError((e as Error).message);
    }
  };

  // В табе «Пользователи» показываем только имя проекта (basename), а не
  // полный путь — полный путь живёт в табе «Проекты». Поддерживаем и \, и /.
  const projectName = (path: string) =>
    path.split(/[\\/]/).filter(Boolean).pop() || path;

  const toggleActive = async () => {
    try {
      await api.adminSetActive(user.user_id, !user.is_active);
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const isSelf = currentUserId != null && currentUserId === user.user_id;

  const toggleAdmin = async () => {
    try {
      await api.adminSetUserAdmin(user.user_id, !user.is_admin);
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  const openPwReset = () => {
    setAction("pw");
    setPwValue("");
    setRowMsg("");
  };

  const savePw = async () => {
    if (pwValue.length < 12) {
      setRowMsg("✗ минимум 12 символов");
      return;
    }
    try {
      await api.adminResetPassword(user.user_id, pwValue);
      setAction(null);
      setPwValue("");
      setStatus("✓ пароль сброшен");
      setTimeout(() => setStatus(""), 3000);
    } catch (e) {
      setRowMsg(`✗ ${(e as Error).message}`);
    }
  };

  const remove = async () => {
    try {
      await api.adminDeleteUser(user.user_id);
      onChanged();
    } catch (e) {
      onError((e as Error).message);
    }
  };

  return (
    <div className="rounded-2xl border border-[var(--border-subtle)] p-4">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold">{user.username || `#${user.user_id}`}</span>
          {user.is_admin ? (
            <span className="text-xs text-emerald-400">админ</span>
          ) : (
            <span className="text-xs text-blue-400">обычный</span>
          )}
          {!user.is_active ? <span className="text-xs text-red-400">отключён</span> : null}
          <KeyBadge user={user} />
        </div>
        <div className="flex items-center gap-2">
          {!isSelf && (
            <button
              onClick={() => void toggleAdmin()}
              className="rounded-lg bg-[var(--bg-hover)] px-2.5 py-1 text-xs text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
            >
              {user.is_admin ? "Разжаловать" : "Сделать админом"}
            </button>
          )}
          <button
            onClick={openPwReset}
            className="rounded-lg bg-[var(--bg-hover)] px-2.5 py-1 text-xs text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
          >
            Сброс пароля
          </button>
          <button
            onClick={() => void toggleActive()}
            className="rounded-lg bg-[var(--bg-hover)] px-2.5 py-1 text-xs text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
          >
            {user.is_active ? "Отключить" : "Включить"}
          </button>
          <button
            onClick={() => {
              setAction("delete");
              setRowMsg("");
            }}
            className="rounded-lg bg-red-900/40 px-2.5 py-1 text-xs text-red-300 hover:bg-red-900/60"
          >
            Удалить
          </button>
        </div>
      </div>

      {action === "pw" && (
        <div className="mb-2 flex flex-wrap items-center gap-2 rounded-xl bg-[var(--bg-sidebar)] p-2">
          <input
            autoFocus
            type="text"
            value={pwValue}
            onChange={(e) => setPwValue(e.target.value)}
            placeholder="новый пароль (мин. 12 символов)"
            className="flex-1 rounded-lg bg-[var(--bg-input)] px-3 py-1.5 text-sm min-w-52"
          />
          <button
            onClick={() => void savePw()}
            className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg-canvas)]"
          >
            Сохранить
          </button>
          <button
            onClick={() => setAction(null)}
            className="rounded-lg bg-[var(--bg-hover)] px-3 py-1.5 text-sm"
          >
            Отмена
          </button>
          {rowMsg && <span className="text-xs text-red-400">{rowMsg}</span>}
        </div>
      )}

      {action === "delete" && (
        <div className="mb-2 flex flex-wrap items-center gap-2 rounded-xl bg-red-900/20 p-2 text-sm">
          <span className="text-red-300">
            Удалить {user.username || `#${user.user_id}`}? Доступы тоже удалятся. Необратимо.
          </span>
          <button
            onClick={() => void remove()}
            className="rounded-lg bg-red-900/50 px-3 py-1.5 text-xs text-red-200 hover:bg-red-900/70"
          >
            Да, удалить
          </button>
          <button
            onClick={() => setAction(null)}
            className="rounded-lg bg-[var(--bg-hover)] px-3 py-1.5 text-xs"
          >
            Отмена
          </button>
        </div>
      )}
      <div className="mb-2 text-sm text-[var(--fg-secondary)]">
        <span className="text-[var(--fg-muted)]">Выданные проекты: </span>
        {access.length ? (
          <span className="inline-flex flex-wrap gap-1.5 align-middle">
            {access.map((a) => (
              <span
                key={a.project_path}
                className="inline-flex items-center gap-1.5 rounded-md bg-[var(--bg-hover)] px-2 py-0.5 text-xs"
              >
                <span className="font-medium text-[var(--fg-primary)]">{projectName(a.project_path)}</span>
                <span className={a.access_level === "readonly" ? "text-amber-400" : "text-emerald-400"}>
                  {a.access_level === "full" ? "полный" : "только чтение"}
                </span>
                <button
                  onClick={() => void revoke(a.project_path)}
                  title="забрать доступ"
                  className="text-[var(--fg-muted)] hover:text-red-400"
                >
                  ✕
                </button>
              </span>
            ))}
          </span>
        ) : (
          "нет"
        )}
      </div>
      <div className="flex flex-wrap items-end gap-2 rounded-xl bg-[var(--bg-sidebar)] p-2">
        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-[var(--fg-muted)]">
          Проект
          <select
            value={effectivePath}
            onChange={(e) => setSel({ ...sel, project_path: e.target.value })}
            className="rounded-lg bg-[var(--bg-input)] px-2 py-1.5 text-sm normal-case text-[var(--fg-primary)]"
          >
            {projects.length === 0 && <option value="">нет проектов</option>}
            {projects.map((p) => (
              <option key={p.id} value={p.abspath} title={p.abspath}>
                {projectName(p.abspath)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-[var(--fg-muted)]">
          Уровень
          <select
            value={sel.access_level}
            onChange={(e) => setSel({ ...sel, access_level: e.target.value as "full" | "readonly" })}
            className="rounded-lg bg-[var(--bg-input)] px-2 py-1.5 text-sm normal-case text-[var(--fg-primary)]"
          >
            <option value="full">полный</option>
            <option value="readonly">только чтение</option>
          </select>
        </label>
        <button
          onClick={() => void grant()}
          disabled={projects.length === 0}
          className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-[var(--bg-canvas)] disabled:opacity-50"
        >
          Выдать доступ
        </button>
        {status && <span className="pb-1.5 text-xs text-emerald-400">{status}</span>}
      </div>
    </div>
  );
}
