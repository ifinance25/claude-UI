/**
 * Вертикальная ручка изменения ширины панели. Сидит на левом крае панели
 * (десктоп). Видимая полоска подсвечивается при наведении/перетаскивании.
 */
export default function ResizeHandle({
  onPointerDown,
}: {
  onPointerDown: (e: React.PointerEvent) => void;
}) {
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label="Перетащите, чтобы изменить ширину панели"
      onPointerDown={onPointerDown}
      className="group absolute left-0 top-0 z-20 hidden h-full w-2 -translate-x-1/2 cursor-col-resize touch-none md:block"
    >
      <div className="mx-auto h-full w-px bg-[var(--border-subtle)] transition-colors group-hover:bg-[var(--accent)]" />
    </div>
  );
}
