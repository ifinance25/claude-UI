import { motion } from "framer-motion";
import { ChatBubbleIcon, FolderIcon } from "@/components/icons";
import type { Project } from "@/lib/types";

export default function NewChatDialog({
  projects, onPick, onClose,
}: {
  projects: Project[];
  onPick: (project: Project | null) => void;
  onClose: () => void;
}) {
  return (
    <motion.div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <motion.div
        className="w-full max-w-md rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
        initial={{ opacity: 0, scale: 0.96 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.96 }}
        transition={{ duration: 0.2, ease: "easeOut" }}
      >
        <div className="mb-3 text-sm font-semibold text-[var(--fg-primary)]">В каком проекте начать чат?</div>
        <div className="max-h-[50vh] space-y-1 overflow-y-auto">
          <button
            onClick={() => onPick(null)}
            className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[15px] text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
          >
            <ChatBubbleIcon size={18} className="shrink-0" />
            <span className="flex-1 truncate">Без проекта (просто Клод)</span>
          </button>
          {projects.map((p) => (
            <button
              key={p.path}
              onClick={() => onPick(p)}
              title={p.path}
              className="flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-[15px] text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
            >
              <FolderIcon size={18} className="shrink-0" />
              <span className="flex-1 truncate">{p.name}</span>
            </button>
          ))}
        </div>
      </motion.div>
    </motion.div>
  );
}
