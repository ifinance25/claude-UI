import { useCallback, useEffect, useRef, useState } from "react";

// Ширина боковой панели (Файлы/Артефакты) на десктопе. Тянется ручкой за левый
// край, запоминается в localStorage. Кнопка-разворот (toggleMax) расширяет
// панель до ~70% окна и обратно — «режим просмотра».
const KEY = "vels.panelWidth";
const DEFAULT = 380;
const MIN = 320;

function maxWidth(): number {
  if (typeof window === "undefined") return 900;
  return Math.max(MIN, Math.round(window.innerWidth * 0.85));
}

function bigWidth(): number {
  if (typeof window === "undefined") return 900;
  return Math.min(Math.round(window.innerWidth * 0.7), maxWidth());
}

function readStored(): number {
  if (typeof window === "undefined") return DEFAULT;
  const raw = Number(window.localStorage.getItem(KEY));
  if (!Number.isFinite(raw) || raw <= 0) return DEFAULT;
  return Math.min(Math.max(raw, MIN), maxWidth());
}

export function usePanelWidth() {
  const [width, setWidth] = useState<number>(readStored);
  const widthRef = useRef(width);
  widthRef.current = width;

  const persist = useCallback((w: number) => {
    try {
      window.localStorage.setItem(KEY, String(w));
    } catch {
      /* приватный режим / квота — ширина просто не запомнится */
    }
  }, []);

  const startResize = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = widthRef.current;
      const max = maxWidth();
      const el = e.currentTarget as HTMLElement;
      const pointerId = e.pointerId;

      const onMove = (ev: PointerEvent) => {
        // Ручка на ЛЕВОМ крае: тянем влево → панель шире.
        const next = Math.min(Math.max(startW + (startX - ev.clientX), MIN), max);
        setWidth(next);
      };
      const onUp = () => {
        el.removeEventListener("pointermove", onMove);
        el.removeEventListener("pointerup", onUp);
        el.removeEventListener("pointercancel", onUp);
        try {
          el.releasePointerCapture(pointerId);
        } catch {
          /* указатель уже отпущен */
        }
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        // Возвращаем iframe'ам интерактивность.
        document.body.classList.remove("is-resizing-panel");
        persist(widthRef.current);
      };

      // Захватываем указатель НА РУЧКУ: события идут ей даже когда курсор
      // заходит на iframe-превью (PDF/HTML/DOCX) в теле панели — иначе drag
      // «залипал» бы (iframe перехватывал pointermove/up). + класс на body
      // глушит pointer-events у iframe'ов на время перетаскивания (страховка).
      try {
        el.setPointerCapture(pointerId);
      } catch {
        /* setPointerCapture не поддержан — слушатели на самом элементе всё равно работают для большинства случаев */
      }
      document.body.classList.add("is-resizing-panel");
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      el.addEventListener("pointermove", onMove);
      el.addEventListener("pointerup", onUp);
      el.addEventListener("pointercancel", onUp);
    },
    [persist],
  );

  const toggleMax = useCallback(() => {
    const big = bigWidth();
    setWidth((w) => {
      const next = w >= big - 4 ? DEFAULT : big;
      persist(next);
      return next;
    });
  }, [persist]);

  // Сжимаем панель, если окно стало уже её текущей ширины.
  useEffect(() => {
    const onResize = () => setWidth((w) => Math.min(w, maxWidth()));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Если панель размонтировали ПОСРЕДИ перетаскивания (onUp не успел сработать) —
  // снимаем глобальные стили/класс, иначе вся страница осталась бы с курсором
  // col-resize, невыделяемой и с «мёртвыми» iframe.
  useEffect(() => {
    return () => {
      if (typeof document !== "undefined") {
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        document.body.classList.remove("is-resizing-panel");
      }
    };
  }, []);

  return { width, startResize, toggleMax };
}
