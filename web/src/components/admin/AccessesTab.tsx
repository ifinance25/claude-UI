import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import type { Access, AdminUser, ProjectAdmin } from "@/lib/types";

// Имя проекта (basename) — полный путь живёт только в табе «Проекты».
const projectName = (path: string | null) =>
  path ? path.split(/[\\/]/).filter(Boolean).pop() || path : "—";

// Сырой текст ошибки ("409 Conflict: {...}") → человекочитаемое сообщение.
const DETAIL_RU: Record<string, string> = {
  "access already exists": "Такой доступ уже выдан этому пользователю",
};
function humanizeError(e: unknown): string {
  if (e instanceof ApiError) {
    let detail = "";
    const brace = e.message.indexOf("{");
    if (brace >= 0) {
      try {
        detail = (JSON.parse(e.message.slice(brace)) as { detail?: string }).detail ?? "";
      } catch {
        /* тело не JSON — оставим пустым */
      }
    }
    if (detail && DETAIL_RU[detail]) return DETAIL_RU[detail];
    switch (e.status) {
      case 409:
        return detail || "Конфликт: запись уже существует";
      case 403:
        return "Недостаточно прав";
      case 404:
        return "Не найдено";
      case 400:
        return detail || "Неверный запрос";
      case 401:
        return "Требуется вход";
      default:
        return detail || `Ошибка ${e.status}`;
    }
  }
  return (e as Error)?.message || "Неизвестная ошибка";
}

export default function AccessesTab() {
  const { user } = useAuth();
  const [accesses, setAccesses] = useState<Access[]>([]);
  const [projects, setProjects] = useState<ProjectAdmin[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ user_id: "", project_id: "", access_level: "full" });
  // Инлайн-подтверждение отзыва (без window.confirm — браузер его блокирует).
  const [confirmId, setConfirmId] = useState<number | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      const [acc, proj, usr] = await Promise.all([
        api.adminListAccesses(),
        api.adminListProjects(),
        api.adminListUsers(),
      ]);
      setAccesses(acc);
      setProjects(proj);
      setUsers(usr);
      setError(null);
    } catch (e) {
      setError(humanizeError(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  const grant = async () => {
    if (!form.user_id || !form.project_id) {
      setError("Выберите пользователя и проект");
      return;
    }
    try {
      await api.adminCreateAccess(
        Number(form.user_id),
        Number(form.project_id),
        form.access_level,
      );
      setForm({ user_id: "", project_id: "", access_level: "full" });
      await reload();
    } catch (e) {
      setError(humanizeError(e));
    }
  };

  const updateLevel = async (id: number, level: string) => {
    try {
      await api.adminUpdateAccess(id, level);
      await reload();
    } catch (e) {
      setError(humanizeError(e));
    }
  };

  const revoke = async (id: number) => {
    setConfirmId(null);
    try {
      await api.adminDeleteAccess(id);
      await reload();
    } catch (e) {
      setError(humanizeError(e));
    }
  };

  return (
    <div>
      {error && (
        <div className="mb-3 flex items-center justify-between gap-2 rounded-xl bg-red-900/30 px-3 py-2 text-sm text-red-300">
          <span>{error}</span>
          <button
            onClick={() => void reload()}
            className="rounded-lg bg-red-900/40 px-2.5 py-1 text-xs hover:bg-red-900/60"
          >
            Повторить
          </button>
        </div>
      )}

      <div className="mb-6 flex flex-wrap items-end gap-2 rounded-2xl border border-[var(--border-subtle)] p-4">
        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-[var(--fg-muted)]">
          Пользователь
          <select
            value={form.user_id}
            onChange={(e) => setForm({ ...form, user_id: e.target.value })}
            className="rounded-lg bg-[var(--bg-input)] px-2 py-1.5 text-sm normal-case text-[var(--fg-primary)]"
          >
            <option value="">выбрать...</option>
            {users.map((u) => (
              <option key={u.user_id} value={u.user_id}>
                {u.username || `#${u.user_id}`}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-[var(--fg-muted)]">
          Проект
          <select
            value={form.project_id}
            onChange={(e) => setForm({ ...form, project_id: e.target.value })}
            className="rounded-lg bg-[var(--bg-input)] px-2 py-1.5 text-sm normal-case text-[var(--fg-primary)]"
          >
            <option value="">выбрать...</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id} title={p.abspath}>
                {projectName(p.abspath)}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wide text-[var(--fg-muted)]">
          Уровень
          <select
            value={form.access_level}
            onChange={(e) => setForm({ ...form, access_level: e.target.value })}
            className="rounded-lg bg-[var(--bg-input)] px-2 py-1.5 text-sm normal-case text-[var(--fg-primary)]"
          >
            <option value="full">полный</option>
            <option value="readonly">только чтение</option>
          </select>
        </label>

        <button
          onClick={() => void grant()}
          className="rounded-lg bg-[var(--accent)] px-4 py-2 text-[var(--bg-canvas)]"
        >
          Выдать доступ
        </button>
      </div>

      <div className="overflow-x-auto rounded-lg border border-[var(--border-subtle)]">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border-subtle)] bg-[var(--bg-sidebar)]">
              <th className="px-4 py-2 text-left font-semibold">ID</th>
              <th className="px-4 py-2 text-left font-semibold">Пользователь</th>
              <th className="px-4 py-2 text-left font-semibold">Проект</th>
              <th className="px-4 py-2 text-left font-semibold">Уровень</th>
              <th className="px-4 py-2 text-left font-semibold">Действие</th>
            </tr>
          </thead>
          <tbody>
            {accesses.map((a) => {
              // Свою строку нельзя менять/отзывать (бэк вернёт 400) — блокируем
              // контролы, чтобы не слать заведомо отказной запрос.
              const isSelf = user?.id === a.user_id;
              return (
                <tr key={a.id} className="border-b border-[var(--border-subtle)] hover:bg-[var(--bg-hover)]">
                  <td className="px-4 py-2">{a.id}</td>
                  <td className="px-4 py-2">
                    {a.username || `#${a.user_id}`}
                    {isSelf && <span className="ml-1 text-xs text-[var(--fg-muted)]">(вы)</span>}
                  </td>
                  <td className="px-4 py-2" title={a.project_path || undefined}>
                    {projectName(a.project_path)}
                  </td>
                  <td className="px-4 py-2">
                    <select
                      value={a.access_level === "readonly" ? "readonly" : "full"}
                      disabled={isSelf}
                      title={isSelf ? "нельзя менять свой доступ" : undefined}
                      onChange={(e) => void updateLevel(a.id, e.target.value)}
                      className="rounded bg-[var(--bg-input)] px-2 py-1 text-xs disabled:opacity-50"
                    >
                      <option value="full">полный</option>
                      <option value="readonly">только чтение</option>
                    </select>
                  </td>
                  <td className="px-4 py-2">
                    {confirmId === a.id ? (
                      <span className="flex items-center gap-1">
                        <button
                          onClick={() => void revoke(a.id)}
                          className="rounded bg-red-900/60 px-2 py-1 text-xs text-red-200 hover:bg-red-900/80"
                        >
                          Да, отозвать
                        </button>
                        <button
                          onClick={() => setConfirmId(null)}
                          className="rounded px-2 py-1 text-xs text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)]"
                        >
                          Отмена
                        </button>
                      </span>
                    ) : (
                      <button
                        onClick={() => setConfirmId(a.id)}
                        disabled={isSelf}
                        title={isSelf ? "нельзя отозвать свой доступ" : undefined}
                        className="rounded bg-red-900/40 px-2 py-1 text-xs text-red-300 hover:bg-red-900/60 disabled:opacity-50 disabled:hover:bg-red-900/40"
                      >
                        Отозвать
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {!loading && accesses.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-[var(--fg-muted)]">
                  Доступов нет
                </td>
              </tr>
            )}
            {loading && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-[var(--fg-muted)]">
                  Загрузка…
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
