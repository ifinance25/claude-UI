import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import { useAuth } from "@/auth/AuthContext";
import UserTab from "@/components/admin/UserTab";
import ProjectsTab from "@/components/admin/ProjectsTab";
import AccessesTab from "@/components/admin/AccessesTab";

type TabType = "users" | "projects" | "accesses";

const TAB_STORAGE_KEY = "admin-last-tab";

function loadInitialTab(): TabType {
  try {
    const saved = localStorage.getItem(TAB_STORAGE_KEY);
    if (saved === "users" || saved === "projects" || saved === "accesses") {
      return saved;
    }
  } catch {
    // localStorage может быть недоступен (приватный режим) — игнорируем.
  }
  return "users";
}

export default function AdminPage() {
  const { user } = useAuth();
  const nav = useNavigate();
  const [tab, setTab] = useState<TabType>(loadInitialTab);

  const selectTab = (t: TabType) => {
    setTab(t);
    try {
      localStorage.setItem(TAB_STORAGE_KEY, t);
    } catch {
      // ignore
    }
  };

  const tabs: Array<{ id: TabType; label: string }> = [
    { id: "users", label: "Пользователи" },
    { id: "projects", label: "Проекты" },
    { id: "accesses", label: "Доступы" },
  ];

  return (
    <div className="min-h-screen bg-[var(--bg-canvas)] text-[var(--fg-primary)]">
      <div className="mx-auto max-w-4xl p-6">
        <div className="mb-6 flex items-center justify-between">
          <h1 className="text-2xl font-semibold">Админ-панель</h1>
          <button
            onClick={() => nav("/")}
            className="rounded-lg bg-[var(--bg-hover)] px-3 py-1.5 text-sm"
          >
            ← В чат
          </button>
        </div>

        {!user?.is_admin && (
          <div className="mb-3 rounded-xl bg-amber-900/30 px-3 py-2 text-sm text-amber-300">
            Нужны права администратора.
          </div>
        )}

        <div className="mb-6 flex gap-2 border-b border-[var(--border-subtle)]">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => selectTab(t.id)}
              className={`px-4 py-2 text-sm font-medium transition-colors ${
                tab === t.id
                  ? "border-b-2 border-[var(--accent)] text-[var(--accent)]"
                  : "text-[var(--fg-secondary)] hover:text-[var(--fg-primary)]"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-6">
          <AnimatePresence mode="wait">
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.18, ease: "easeInOut" }}
            >
              {tab === "users" && <UserTab />}
              {tab === "projects" && <ProjectsTab />}
              {tab === "accesses" && <AccessesTab />}
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
