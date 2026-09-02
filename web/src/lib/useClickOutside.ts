import { useEffect, type RefObject } from "react";

/**
 * Закрывает попап/дропдаун при клике вне элемента ref.
 * Заменяет три копии mousedown-listener в ModelSelector / InputModelButton / PromptPicker.
 */
export function useClickOutside<T extends HTMLElement>(
  ref: RefObject<T>,
  onOutside: () => void,
  enabled: boolean = true,
): void {
  useEffect(() => {
    if (!enabled) return;
    const handler = (event: MouseEvent) => {
      const el = ref.current;
      if (!el) return;
      if (el.contains(event.target as Node)) return;
      onOutside();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [ref, onOutside, enabled]);
}
