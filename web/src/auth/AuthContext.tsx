import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, ApiError, AUTH_UNAUTHORIZED_EVENT } from "@/api/client";
import type { ApiUser } from "@/lib/types";

interface AuthCtx {
  user: ApiUser | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<ApiUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      setUser(await api.me());
      setError(null);
    } catch (e) {
      // Only 401 means "not logged in" — clear the user so the app
      // redirects to /login. Network errors and 5xx leave the cached
      // user state intact and surface as `error` so transient backend
      // restarts don't kick the user out of a working session.
      if (e instanceof ApiError && e.status === 401) {
        setUser(null);
        setError(null);
      } else {
        setError((e as Error).message);
      }
    }
    setLoading(false);
  };

  const logout = async () => {
    await api.logout();
    setUser(null);
  };

  useEffect(() => {
    void refresh();
  }, []);

  // Любой 401 (кроме явных auth-флоу) посреди работы → сброс user, чтобы
  // ProtectedRoute увёл на /login, а не оставлял залипший баннер ошибки.
  useEffect(() => {
    const onUnauthorized = () => setUser(null);
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  return (
    <Ctx.Provider value={{ user, loading, error, refresh, logout }}>
      {children}
    </Ctx.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
