import { ClaudeLogo } from "@/components/icons";

export default function Logo() {
  return (
    <div className="flex items-center gap-2.5 px-1">
      {/* Знак Claude на тёплой плитке. Терракота: знак и кнопки входа. */}
      <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--bg-hover)]">
        <ClaudeLogo size={20} />
      </div>
      <span className="font-display text-[1.35rem] font-normal tracking-tight text-[var(--fg-primary)]">
        AI-Panel
      </span>
    </div>
  );
}
