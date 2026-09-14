import { ClaudeLogo } from "@/components/icons";

export default function Logo() {
  return (
    <div className="flex items-center gap-2.5 px-1">
      {/* Настоящий знак Claude на нейтральной плитке (bg-hover) — та же плитка,
          что у аватара, кружка пустого экрана и бейджа модели. Терракота живёт
          только в самом знаке: интерфейс монохромный, один акцентный цвет. */}
      <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--bg-hover)]">
        <ClaudeLogo size={20} />
      </div>
      <span className="font-display text-lg font-semibold text-[var(--fg-primary)]">Vels Claude</span>
    </div>
  );
}
