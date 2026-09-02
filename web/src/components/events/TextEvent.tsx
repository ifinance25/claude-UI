import { useMemo } from "react";
import { Markdown } from "@/lib/Markdown";
import { extractThinking } from "@/lib/stripThinking";
import { ThinkingEvent } from "@/components/events/ThinkingEvent";
import { useTypewriter } from "@/lib/useTypewriter";

export function TextEvent({
  content,
  reasoningOn = false,
  live = false,
}: {
  content: string;
  /** Показывать ли блок «Размышления» (тоггл-лампочка в шапке). Выкл → блока
   *  нет вовсе (чистый чат); вкл → блок есть, но СВЁРНУТ (разворачивается
   *  кликом), чтобы он не «выскакивал» в конце ответа. */
  reasoningOn?: boolean;
  live?: boolean;
}) {
  const { visible, thinking } = useMemo(
    () => extractThinking(content),
    [content],
  );
  const shownVisible = useTypewriter(visible, live);
  // Пока бабл ЖИВОЙ, мысли владеет живая панель (LiveActivityPanel) — инлайновый
  // исторический блок «Размышления» показываем только после завершения хода,
  // иначе при verbose ≥ 2 мысли двоились бы (панель + блок).
  const showBlock = reasoningOn && !live && thinking.length > 0;
  if (!shownVisible.trim() && !showBlock) return null;
  return (
    <div className="my-4">
      {showBlock && (
        <ThinkingEvent blocks={thinking} defaultOpen={false} />
      )}
      {shownVisible.trim() && (
        <Markdown>
          {shownVisible.length < visible.length
            ? shownVisible + "▍"
            : shownVisible}
        </Markdown>
      )}
    </div>
  );
}
