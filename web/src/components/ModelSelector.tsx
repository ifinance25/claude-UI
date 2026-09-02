import { useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronDownIcon, providerLogo } from "@/components/icons";
import { useClickOutside } from "@/lib/useClickOutside";
import { useModelInfo } from "@/lib/useModelInfo";

interface Props {
  /** Called whenever the model is changed so callers can show a toast. */
  onChange?: (model: string) => void;
}

export default function ModelSelector({ onChange }: Props) {
  const { info, setModel } = useModelInfo(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useClickOutside(wrapRef, () => setOpen(false), open);

  const pick = async (id: string) => {
    if (!info || id === info.current || busy) {
      setOpen(false);
      return;
    }
    setBusy(true);
    try {
      const updated = await setModel(id);
      onChange?.(updated.current);
      setOpen(false);
    } catch {
      // keep dropdown open so user can retry; nothing else to do here
    } finally {
      setBusy(false);
    }
  };

  const current = info?.current ?? "—";
  const knownItem = info?.known.find((m) => m.id === current);
  const display = knownItem?.label ?? current;

  return (
    <div ref={wrapRef} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={!info || busy}
        className="flex items-center gap-2 rounded-xl px-3 py-2 text-sm text-[var(--fg-primary)] hover:bg-[var(--bg-hover)] disabled:opacity-60"
        title="Сменить модель Claude"
      >
        <span className="flex items-center gap-2">
          {providerLogo(current, 16)}
          <span className="max-w-[200px] truncate font-medium" title={display}>
            {display}
          </span>
        </span>
        <ChevronDownIcon size={16} />
      </button>
      <AnimatePresence>
        {open && info && (
        <motion.div
          initial={{ opacity: 0, y: 8, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 8, scale: 0.98 }}
          transition={{ duration: 0.16, ease: "easeOut" }}
          className="absolute left-0 z-20 mt-2 w-72 origin-top overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] shadow-lg"
        >
          {info.known.map((m) => {
            const active = m.id === info.current;
            return (
              <button
                key={m.id}
                onClick={() => void pick(m.id)}
                className={`flex w-full items-start gap-3 px-4 py-3 text-left transition-colors ${
                  active
                    ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                    : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                }`}
              >
                <span
                  className={`mt-1 inline-block h-2 w-2 rounded-full ${
                    active ? "bg-emerald-500" : "bg-transparent"
                  }`}
                />
                <span className="mt-0.5">{providerLogo(m.id, 16)}</span>
                <span className="flex-1">
                  <span className="block text-sm font-semibold">{m.label}</span>
                  <span className="mt-0.5 block text-xs text-[var(--fg-muted)]">
                    {m.hint}
                  </span>
                </span>
              </button>
            );
          })}
          <div className="border-t border-[var(--border-subtle)] px-4 py-2 text-[11px] text-[var(--fg-muted)]">
            Применится со следующего сообщения · режим разрешений:{" "}
            <code>{info.permission_mode}</code>
          </div>
        </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
