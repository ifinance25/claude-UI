import { motion } from "framer-motion";
import { CloseIcon } from "@/components/icons";
import type { SlashCommand } from "@/lib/types";

export default function CommandsPopover({
  title,
  items,
  onPick,
  onClose,
}: {
  title: string;
  items: SlashCommand[];
  onPick: (cmd: string) => void;
  onClose: () => void;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: 8, scale: 0.98 }}
      transition={{ duration: 0.16, ease: "easeOut" }}
      className="absolute bottom-full left-0 right-0 mb-2 origin-bottom overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] shadow-lg"
    >
      <div className="flex items-center justify-between px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
        <span>{title}</span>
        <button
          onClick={onClose}
          className="icon-btn transition-colors hover:text-[var(--fg-primary)]"
          title="Закрыть"
          aria-label="Закрыть"
        >
          <CloseIcon size={16} />
        </button>
      </div>
      <div className="max-h-72 overflow-y-auto">
        {items.length === 0 && (
          <div className="px-4 py-3 text-sm text-[var(--fg-muted)]">Пусто</div>
        )}
        {items.map((s) => (
          <button
            key={s.cmd}
            onClick={() => onPick(s.cmd)}
            className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm text-[var(--fg-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
          >
            <span className="font-mono text-[13px]">{s.cmd}</span>
            <span className="flex-1 truncate text-[var(--fg-muted)]">{s.label}</span>
          </button>
        ))}
      </div>
    </motion.div>
  );
}
