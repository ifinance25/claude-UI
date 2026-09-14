import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ProjectAdmin } from "@/lib/types";

export default function ProjectsTab() {
  const [projects, setProjects] = useState<ProjectAdmin[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ abspath: "" });
  const [editId, setEditId] = useState<number | null>(null);
  const [editPath, setEditPath] = useState("");
  // Инлайн-подтверждение удаления (без window.confirm — браузер его блокирует).
  const [confirmId, setConfirmId] = useState<number | null>(null);

  const reload = async () => {
    setLoading(true);
    try {
      setProjects(await api.adminListProjects());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void reload();
  }, []);

  const create = async () => {
    if (!form.abspath.trim()) {
      setError("❌ Имя проекта не может быть пустым");
      return;
    }
    try {
      await api.adminCreateProject(form.abspath.trim());
      setForm({ abspath: "" });
      setError(null);
      await reload();
    } catch (e) {
      const msg = (e as Error).message;
      setError(`❌ ${msg}`);
    }
  };

  const startEdit = (p: ProjectAdmin) => {
    setEditId(p.id);
    setEditPath(p.abspath);
  };

  const saveEdit = async () => {
    if (!editPath.trim() || editId === null) return;
    try {
      await api.adminUpdateProject(editId, editPath.trim());
      setEditId(null);
      setEditPath("");
      await reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const cancelEdit = () => {
    setEditId(null);
    setEditPath("");
  };

  const remove = async (id: number) => {
    setConfirmId(null);
    try {
      await api.adminDeleteProject(id);
      await reload();
    } catch (e) {
      setError((e as Error).message);
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
        <input
          placeholder="Имя проекта, например CRM"
          value={form.abspath}
          onChange={(e) => setForm({ abspath: e.target.value })}
          className="flex-1 rounded-lg bg-[var(--bg-input)] px-3 py-2 min-w-60"
        />
        <button
          onClick={() => void create()}
          className="rounded-lg bg-[var(--accent)] px-4 py-2 text-[var(--bg-canvas)]"
        >
          Создать проект
        </button>
      </div>

      <div className="space-y-2">
        {projects.map((p) => (
          <div key={p.id} className="rounded-lg border border-[var(--border-subtle)] p-3">
            {editId === p.id ? (
              <div className="flex flex-wrap items-end gap-2">
                <input
                  autoFocus
                  value={editPath}
                  onChange={(e) => setEditPath(e.target.value)}
                  className="flex-1 rounded-lg bg-[var(--bg-input)] px-3 py-2 min-w-60"
                />
                <button
                  onClick={() => void saveEdit()}
                  className="rounded-lg bg-emerald-900/40 px-3 py-2 text-sm text-emerald-300 hover:bg-emerald-900/60"
                >
                  Сохранить
                </button>
                <button
                  onClick={cancelEdit}
                  className="rounded-lg bg-[var(--bg-hover)] px-3 py-2 text-sm"
                >
                  Отмена
                </button>
              </div>
            ) : (
              <div className="flex items-center justify-between gap-2">
                <div className="flex-1 break-all">
                  <div className="font-mono text-sm text-[var(--fg-primary)]">{p.abspath}</div>
                  <div className="text-xs text-[var(--fg-muted)]">ID: {p.id}</div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => startEdit(p)}
                    className="rounded-lg bg-[var(--bg-hover)] px-2.5 py-1 text-xs text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
                  >
                    Переименовать
                  </button>
                  {confirmId === p.id ? (
                    <>
                      <button
                        onClick={() => void remove(p.id)}
                        className="rounded-lg bg-red-900/60 px-2.5 py-1 text-xs text-red-200 hover:bg-red-900/80"
                      >
                        Да, удалить
                      </button>
                      <button
                        onClick={() => setConfirmId(null)}
                        className="rounded-lg bg-[var(--bg-hover)] px-2.5 py-1 text-xs text-[var(--fg-secondary)]"
                      >
                        Отмена
                      </button>
                    </>
                  ) : (
                    <button
                      onClick={() => setConfirmId(p.id)}
                      className="rounded-lg bg-red-900/40 px-2.5 py-1 text-xs text-red-300 hover:bg-red-900/60"
                    >
                      Удалить
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        ))}
        {loading && (
          <div className="rounded-lg border border-dashed border-[var(--border-subtle)] p-6 text-center text-[var(--fg-muted)]">
            Загрузка…
          </div>
        )}
        {!loading && projects.length === 0 && (
          <div className="rounded-lg border border-dashed border-[var(--border-subtle)] p-6 text-center text-[var(--fg-muted)]">
            Проектов нет
          </div>
        )}
      </div>
    </div>
  );
}
