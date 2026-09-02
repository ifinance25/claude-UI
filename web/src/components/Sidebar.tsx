import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { api } from "@/api/client";
import { decideNewChat } from "@/lib/newSession";
import { useAuth } from "@/auth/AuthContext";
import {
  NewChatIcon,
  PanelLeftIcon,
  SearchIcon,
  SettingsIcon,
  TrashIcon,
} from "@/components/icons";
import Logo from "@/components/Logo";
import { sessionTitle } from "@/lib/sessionTitle";
import type { Project, Session } from "@/lib/types";

interface Props {
  activeSessionUuid: string | null;
  onSelectSession: (s: Session | null) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onOpenSettings: () => void;
}

const SEARCH_DEBOUNCE_MS = 200;

interface DateGroup {
  label: string;
  sessions: Session[];
}

function groupByDate(sessions: Session[]): DateGroup[] {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const dayMs = 24 * 60 * 60 * 1000;
  const buckets: Record<string, Session[]> = {
    "Сегодня": [],
    "Вчера": [],
    "Последние 7 дней": [],
    "Последние 30 дней": [],
    "Раньше": [],
  };
  const sorted = [...sessions].sort(
    (a, b) => new Date(b.last_activity).getTime() - new Date(a.last_activity).getTime()
  );
  for (const s of sorted) {
    const ts = new Date(s.last_activity).getTime();
    if (ts >= startOfToday) buckets["Сегодня"].push(s);
    else if (ts >= startOfToday - dayMs) buckets["Вчера"].push(s);
    else if (ts >= startOfToday - 7 * dayMs) buckets["Последние 7 дней"].push(s);
    else if (ts >= startOfToday - 30 * dayMs) buckets["Последние 30 дней"].push(s);
    else buckets["Раньше"].push(s);
  }
  return Object.entries(buckets)
    .filter(([, arr]) => arr.length > 0)
    .map(([label, arr]) => ({ label, sessions: arr }));
}

// Тонкая обёртка для плавного появления строк списка (диалоги, папки).
// Лёгкий fade + сдвиг вверх на 4px. Стаггер по индексу, но только для
// первых ~8 строк — дальше задержка обнуляется, чтобы длинные списки не
// «доезжали» секундами.
//
// ВАЖНО: эта анимация НЕ должна переигрываться на 4-секундном фоновом
// reload(). framer-motion переигрывает initial→animate только при
// МОНТИРОВАНИИ. Строки имеют стабильные ключи (session_uuid / p.path),
// поэтому ререндер с теми же ключами не ремоунтит элемент → анимация
// не повторяется. Стаггер-задержка тоже не «мигает».
function MotionItem({ index = 0, children }: { index?: number; children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: "easeOut", delay: Math.min(index, 8) * 0.02 }}
    >
      {children}
    </motion.div>
  );
}

function SessionRow({
  session,
  active,
  onClick,
  onDelete,
}: {
  session: Session;
  active: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  const preview = sessionTitle(session);
  const title = preview;

  const handleDeleteClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onDelete();
  };

  return (
    <div
      onClick={onClick}
      title={title}
      className={`group flex w-full cursor-pointer items-center gap-2 rounded-xl px-3 py-2.5 text-[15px] transition-colors ${
        active
          ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
          : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
      }`}
    >
      {session.is_running && (
        <span
          className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-emerald-500"
          title="Клод работает в этом диалоге"
        />
      )}
      <span className="flex-1 truncate">{preview}</span>
      <button
        onClick={handleDeleteClick}
        className="shrink-0 rounded-lg p-1.5 opacity-0 transition-opacity hover:text-red-400 group-hover:opacity-60 hover:!opacity-100"
        title="Удалить сессию"
        aria-label="Удалить сессию"
      >
        <TrashIcon size={16} />
      </button>
    </div>
  );
}

export default function Sidebar({
  activeSessionUuid,
  onSelectSession,
  collapsed,
  onToggleCollapsed,
  onOpenSettings,
}: Props) {
  const { user } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  // Пустой список projects значит либо «ещё не загрузились», либо «их нет».
  // Без этого различия кнопка «Новый чат», нажатая до загрузки, создавала
  // сессию с project_path=null — Claude уходил работать в data/scratch, и
  // привязать проект такому чату уже нельзя.
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const debounceRef = useRef<number | null>(null);

  const initialSelectionDoneRef = useRef(false);

  const reload = async (q: string = activeQuery) => {
    try {
      const [p, s] = await Promise.all([api.listProjects(), api.listSessions(q)]);
      setProjects(p);
      setProjectsLoaded(true);
      setSessions(s);
      setError(null);
      // После первой загрузки авто-выбираем самую свежую сессию, если
      // юзер ничего не открыл. Так после reload страница не зависает на
      // «Выберите чат», а сразу показывает последний разговор.
      if (
        !initialSelectionDoneRef.current
        && !activeSessionUuid
        && s.length > 0
      ) {
        initialSelectionDoneRef.current = true;
        const newest = [...s].sort(
          (a, b) =>
            new Date(b.last_activity).getTime() -
            new Date(a.last_activity).getTime(),
        )[0];
        onSelectSession(newest);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  useEffect(() => {
    void reload(activeQuery);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeQuery]);

  // Периодически обновляем список сессий, чтобы индикатор is_running
  // («Клод работает») оставался актуальным без перезагрузки страницы.
  useEffect(() => {
    const id = window.setInterval(() => {
      void reload(activeQuery);
    }, 4000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeQuery]);

  useEffect(() => {
    if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      setActiveQuery(searchInput.trim());
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
    };
  }, [searchInput]);

  const newSession = async (project: Project | null) => {
    try {
      const s = await api.createSession(project?.path ?? null, project?.name ?? null);
      setSearchInput("");
      setActiveQuery("");
      setSessions((arr) => [...arr, s]);
      onSelectSession(s);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const deleteSession = async (target: Session) => {
    try {
      await api.deleteSession(target.session_uuid);
      const remaining = sessions.filter(
        (s) => s.session_uuid !== target.session_uuid
      );
      setSessions(remaining);
      // если удалили активную — переключаемся на самую свежую из оставшихся,
      // иначе сбрасываем active чтобы показать заглушку «выберите чат».
      if (activeSessionUuid === target.session_uuid) {
        const newest =
          remaining.length > 0
            ? [...remaining].sort(
                (a, b) =>
                  new Date(b.last_activity).getTime() -
                  new Date(a.last_activity).getTime(),
              )[0]
            : null;
        onSelectSession(newest);
      }
    } catch (e) {
      setError(`Не удалось удалить: ${(e as Error).message}`);
    }
  };

  // В light-версии проект ровно один — выбирать не из чего, диалог не нужен.
  // Но создавать чат до того, как список проектов загрузился, нельзя: сессия
  // уйдёт в scratch безвозвратно. Пока грузится — кнопка недоступна; если
  // проектов действительно нет — говорим об этом вместо тихого scratch.
  const onNewChat = () => {
    const decision = decideNewChat(projects, projectsLoaded);
    if (decision.action === "wait") return;
    if (decision.action === "no-projects") {
      setError(
        "Проект не подключён: положите папку проекта внутрь PROJECTS_DIR и перезапустите сервис.",
      );
      return;
    }
    void newSession(decision.project as Project);
  };

  // Список показывает все диалоги единственного проекта.
  const dateGroups = useMemo(() => groupByDate(sessions), [sessions]);

  // Свёрнутая мини-панель: только две иконки (развернуть + новый чат).
  const collapsedContent = (
    <div className="flex flex-col items-center py-4">
      <button
        onClick={onToggleCollapsed}
        className="rounded-xl p-2.5 text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
        title="Развернуть боковую панель"
      >
        <PanelLeftIcon size={22} />
      </button>
      <button
        onClick={onNewChat}
        disabled={!projectsLoaded}
        className="mt-2 rounded-xl p-2.5 text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
        title="Новый чат"
      >
        <NewChatIcon size={22} />
      </button>
    </div>
  );

  // Полная панель: бренд, новый чат, поиск, папки/дерево, группы по дате,
  // админ-ссылка, профиль. Фиксированная ширина w-80 (320px) внутри
  // motion.aside, чтобы при анимации ширины контент не «дёргался».
  const fullContent = (
    <div className="flex h-full w-80 flex-col">
      {/* Top: brand + collapse */}
      <div className="flex items-center justify-between px-4 pt-4 pb-2">
        <Logo />
        <button
          onClick={onToggleCollapsed}
          className="rounded-xl p-2 text-[var(--fg-muted)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
          title="Свернуть боковую панель"
        >
          <PanelLeftIcon size={20} />
        </button>
      </div>

      {/* Новый чат */}
      <div className="px-4 pt-2">
        <button
          onClick={onNewChat}
          disabled={!projectsLoaded}
          className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-[15px] font-medium text-[var(--fg-primary)] hover:bg-[var(--bg-hover)]"
        >
          <NewChatIcon size={20} />
          <span>Новый чат</span>
        </button>
      </div>

      {/* Поиск */}
      <div className="px-4 pt-1.5">
        <div className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-[15px] text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)]">
          <SearchIcon size={20} />
          <input
            type="search"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            placeholder="Поиск"
            className="flex-1 bg-transparent placeholder:text-[var(--fg-muted)] focus:outline-none"
          />
        </div>
      </div>

      {error && (
        <div className="mx-4 mt-2 rounded-xl bg-red-900/30 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Прокручиваемая середина */}
      <div className="flex-1 overflow-y-auto px-4 pt-4 pb-3">

        {/* Чаты сгруппированы по дате */}
        <div className="mt-5 space-y-4">
          {dateGroups.length === 0 && (
            <div className="px-3 text-sm text-[var(--fg-muted)]">
              {activeQuery
                ? `Нет чатов по запросу «${activeQuery}».`
                : "Чатов пока нет. Создайте новый чат сверху."}
            </div>
          )}
          {dateGroups.map((g) => (
            <div key={g.label}>
              <div className="px-3 pb-1.5 text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
                {g.label}
              </div>
              <div className="space-y-1">
                {g.sessions.map((s, si) => (
                  <MotionItem key={s.session_uuid} index={si}>
                    <SessionRow
                      session={s}
                      active={s.session_uuid === activeSessionUuid}
                      onClick={() => onSelectSession(s)}
                      onDelete={() => void deleteSession(s)}
                    />
                  </MotionItem>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>


      {/* Профиль пользователя — открывает модал настроек */}
      <div className="border-t border-[var(--border-subtle)] p-3">
        <button
          onClick={onOpenSettings}
          className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-[15px] text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
        >
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[var(--bg-hover)] text-xs font-semibold text-[var(--fg-primary)]">
            {(user?.username || String(user?.id ?? "?")).slice(0, 1).toUpperCase()}
          </div>
          <span className="flex-1 truncate text-left">
            {user?.username || `#${user?.id ?? "?"}`}
          </span>
          <SettingsIcon size={18} />
        </button>
      </div>
    </div>
  );

  return (
    <motion.aside
      animate={{ width: collapsed ? 64 : 320 }}
      transition={{ duration: 0.28, ease: "easeOut" }}
      initial={false}
      className="flex h-screen shrink-0 flex-col overflow-hidden bg-[var(--bg-sidebar)]"
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={collapsed ? "collapsed" : "full"}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="flex h-full flex-col items-center"
        >
          {collapsed ? collapsedContent : fullContent}
        </motion.div>
      </AnimatePresence>
    </motion.aside>
  );
}
