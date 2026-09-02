import { useEffect, useState } from "react";
import Chat from "@/components/Chat";
import SettingsModal from "@/components/SettingsModal";
import Sidebar from "@/components/Sidebar";
import { startNewSession } from "@/lib/newSession";
import type { Session } from "@/lib/types";

const COLLAPSED_KEY = "vels.sidebarCollapsed";

export default function ChatPage() {
  const [active, setActive] = useState<Session | null>(null);
  // Persist collapsed state across reloads — иначе юзер каждый раз
  // открывает страницу с collapsed=false и не понимает почему его
  // настройка не сохранилась.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(COLLAPSED_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
    } catch {
      /* localStorage может быть недоступен — игнорируем */
    }
  }, [collapsed]);

  return (
    <div className="flex h-screen">
      <Sidebar
        activeSessionUuid={active?.session_uuid ?? null}
        onSelectSession={setActive}
        collapsed={collapsed}
        onToggleCollapsed={() => setCollapsed((v) => !v)}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {active ? (
        <Chat
          session={active}
          key={active.session_uuid}
          onNewChat={() =>
            void startNewSession(
              { path: active.project_path, name: active.project_name },
              setActive,
            )
          }
        />
      ) : (
        <main className="flex flex-1 flex-col bg-[var(--bg-canvas)]">
          <div className="flex flex-1 items-center justify-center px-6">
            <div className="text-center">
              <div className="text-xl font-semibold text-[var(--fg-primary)]">
                Выберите чат или создайте новый
              </div>
              <div className="mt-2 text-base text-[var(--fg-muted)]">
                Откройте сессию слева — или нажмите <strong>Новый чат</strong>.
              </div>
            </div>
          </div>
        </main>
      )}

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
      />
    </div>
  );
}
