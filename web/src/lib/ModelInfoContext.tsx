import {
  createContext,
  useCallback,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api } from "@/api/client";
import type { ModelInfo } from "@/lib/types";

export interface UseModelInfoResult {
  info: ModelInfo | null;
  /** Сообщение об ошибке загрузки, если api.getModel() провалился. */
  error: string | null;
  /** true до завершения первой загрузки. */
  loading: boolean;
  /** Сменить модель. Возвращает обновлённый ModelInfo. */
  setModel: (id: string) => Promise<ModelInfo>;
  /** Принудительно перечитать с бэка (например, при открытии модала). */
  refetch: () => Promise<void>;
}

export const ModelInfoContext = createContext<UseModelInfoResult | null>(null);

/**
 * Provider держит ОДИН экземпляр state'а модели на всё дерево —
 * InputModelButton, ModelSelector, SettingsModal автоматически
 * синхронизированы через React-context. Без module-level Set'ов и
 * pub-sub-листенеров (которые ломали тест-изоляцию и при HMR могли
 * сетать state в размонтированные fiber'ы).
 */
export function ModelInfoProvider({ children }: { children: ReactNode }) {
  const [info, setInfo] = useState<ModelInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refetch = useCallback(async () => {
    try {
      const data = await api.getModel();
      setInfo(data);
      setError(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    // Первая загрузка с РЕТРАЯМИ: сразу после входа cookie-сессия может быть ещё
    // не готова (гонка редиректа), и одиночный 401 навсегда оставлял бы
    // «Не удалось загрузить модель». Пробуем несколько раз, прежде чем сдаться.
    const load = async (attempt: number): Promise<void> => {
      try {
        const data = await api.getModel();
        if (cancelled) return;
        setInfo(data);
        setError(null);
        setLoading(false);
      } catch (e) {
        if (cancelled) return;
        if (attempt < 3) {
          retryTimer = setTimeout(() => void load(attempt + 1), 1200);
          return;
        }
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        setLoading(false);
      }
    };
    void load(0);
    return () => {
      cancelled = true;
      // Снимаем запланированный ретрай — иначе после размонтирования он бы
      // выстрелил лишним fetch'ем (находка аудита #4).
      if (retryTimer !== null) clearTimeout(retryTimer);
    };
  }, []);

  const setModel = useCallback(async (id: string): Promise<ModelInfo> => {
    const updated = await api.patchModel(id);
    setInfo(updated);
    // Успешная смена снимает любое прежнее предупреждение (stale loadError от
    // гонки на старте) — иначе оно висит, и кажется, что «не переключается».
    setError(null);
    return updated;
  }, []);

  return (
    <ModelInfoContext.Provider value={{ info, error, loading, setModel, refetch }}>
      {children}
    </ModelInfoContext.Provider>
  );
}
