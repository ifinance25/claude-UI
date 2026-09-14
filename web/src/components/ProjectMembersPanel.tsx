import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ProjectMember } from "@/lib/types";

interface Props {
  projectId: number;
}

// ApiError.message is "<status> <statusText>: <body>", where the body is
// usually a FastAPI {"detail": "..."} JSON. Pull the detail out so the user
// sees a clean sentence (e.g. "Пользователь не найден…") instead of the raw
// HTTP envelope. Mirrors ApiKeyPanel.humanError.
function humanError(e: unknown): string {
  const msg = (e as Error)?.message ?? String(e);
  const brace = msg.indexOf("{");
  if (brace !== -1) {
    try {
      const parsed = JSON.parse(msg.slice(brace)) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* not JSON — fall through to the raw message */
    }
  }
  return msg;
}

export default function ProjectMembersPanel({ projectId }: Props) {
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [identifier, setIdentifier] = useState("");
  const [level, setLevel] = useState<"full" | "readonly">("readonly");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setMembers(await api.listMembers(projectId));
      setError(null);
    } catch (e) {
      setError(humanError(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const add = async () => {
    const ident = identifier.trim();
    if (!ident) return;
    setLoading(true);
    setError(null);
    try {
      await api.addMember(projectId, ident, level);
      setIdentifier("");
      await load();
    } catch (e) {
      setError(humanError(e));
      setLoading(false);
    }
  };

  const changeLevel = async (uid: number, next: "full" | "readonly") => {
    setLoading(true);
    setError(null);
    try {
      await api.updateMember(projectId, uid, next);
      await load();
    } catch (e) {
      setError(humanError(e));
      setLoading(false);
    }
  };

  const remove = async (uid: number) => {
    setLoading(true);
    setError(null);
    try {
      await api.removeMember(projectId, uid);
      await load();
    } catch (e) {
      setError(humanError(e));
      setLoading(false);
    }
  };

  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold text-[var(--fg-primary)]">
        Участники проекта
      </h3>

      {error && (
        <div className="rounded-xl border border-red-700/40 bg-red-900/20 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Список участников */}
      <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-2 text-sm">
        {members.length === 0 ? (
          <div className="px-2 py-3 text-[var(--fg-muted)]">
            {loading ? "Загрузка…" : "Пока никого"}
          </div>
        ) : (
          <ul className="divide-y divide-[var(--border-subtle)]">
            {members.map((m) => (
              <li
                key={m.user_id}
                className="flex items-center gap-2 px-2 py-2"
              >
                <span className="min-w-0 flex-1 truncate text-[var(--fg-primary)]">
                  {m.username ? `@${m.username}` : m.user_id}
                </span>
                {m.manageable ? (
                  <>
                    <select
                      value={m.access_level}
                      onChange={(e) =>
                        void changeLevel(
                          m.user_id,
                          e.target.value as "full" | "readonly",
                        )
                      }
                      disabled={loading}
                      aria-label="уровень доступа"
                      className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-input)] px-2 py-1 text-sm text-[var(--fg-primary)] focus:outline-none disabled:opacity-50"
                    >
                      <option value="full">полный</option>
                      <option value="readonly">только чтение</option>
                    </select>
                    <button
                      onClick={() => void remove(m.user_id)}
                      disabled={loading}
                      aria-label="убрать"
                      className="rounded-lg px-2.5 py-1 text-sm text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)] disabled:opacity-50"
                    >
                      убрать
                    </button>
                  </>
                ) : (
                  <span className="text-[var(--fg-muted)]">
                    {m.access_level === "full" ? "полный" : "только чтение"}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Добавить участника */}
      <div className="space-y-2">
        <label
          htmlFor="member-identifier"
          className="block text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]"
        >
          Добавить участника
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <input
            id="member-identifier"
            value={identifier}
            onChange={(e) => setIdentifier(e.target.value)}
            placeholder="Telegram ID или @username"
            disabled={loading}
            className="min-w-0 flex-1 rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-input)] px-3 py-2 text-sm text-[var(--fg-primary)] focus:outline-none disabled:opacity-50"
          />
          <select
            value={level}
            onChange={(e) => setLevel(e.target.value as "full" | "readonly")}
            disabled={loading}
            aria-label="уровень нового участника"
            className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-input)] px-2 py-2 text-sm text-[var(--fg-primary)] focus:outline-none disabled:opacity-50"
          >
            <option value="readonly">только чтение</option>
            <option value="full">полный</option>
          </select>
          <button
            onClick={() => void add()}
            disabled={loading || !identifier.trim()}
            className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--bg-canvas)] disabled:opacity-50"
          >
            Добавить
          </button>
        </div>
      </div>
    </div>
  );
}
