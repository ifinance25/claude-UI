import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";

interface Props {
  /**
   * Owner/admin caller from the JWT (`is_admin`). Used only as a fallback for
   * the helper text until the backend GET resolves — the server-reported
   * `privileged` flag (which also honors the whitelist) takes precedence.
   */
  privileged: boolean;
}

// ApiError.message is "<status> <statusText>: <body>", where the body is
// usually a FastAPI {"detail": "..."} JSON. Pull the detail out so the user
// sees a clean sentence instead of the raw HTTP envelope.
function humanError(e: unknown): string {
  const msg = (e as Error)?.message ?? String(e);
  const brace = msg.indexOf("{");
  if (brace !== -1) {
    try {
      const parsed = JSON.parse(msg.slice(brace)) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* not JSON — fall through to the raw message */
    }
  }
  return msg;
}

export default function ApiKeyPanel({ privileged }: Props) {
  // Server-side metadata for the currently stored key (never the key itself).
  const [status, setStatus] = useState<string | null>(null);
  const [last4, setLast4] = useState<string | null>(null);
  // Server-reported privileged flag (honors whitelist, not just JWT is_admin).
  // null until the GET resolves, at which point it supersedes the prop.
  const [serverPrivileged, setServerPrivileged] = useState<boolean | null>(null);
  // Controlled password input for a new key.
  const [key, setKey] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  // Set when the backend answers 501 (CONNECTIONS_SECRET_KEY unset → dormant).
  const [disabled, setDisabled] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const meta = await api.getApiKey();
      setStatus(meta.status);
      setLast4(meta.last4);
      setServerPrivileged(meta.privileged ?? null);
      setError(null);
      setDisabled(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 501) {
        setDisabled(true);
      } else {
        setError(humanError(e));
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    const trimmed = key.trim();
    setSuccess(null);
    if (!trimmed) {
      setError("Введите ключ");
      return;
    }
    // Offline format check mirrors the backend (sk-ant- prefix). Saves a
    // round-trip on obvious typos; the server re-validates and live-probes.
    if (!trimmed.startsWith("sk-ant-")) {
      setError("Ключ должен начинаться с sk-ant-");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await api.putApiKey(trimmed);
      setKey("");
      // res.warning is set for an unverified save (network error during probe).
      setSuccess(res.warning ? `${res.message}. ${res.warning}` : res.message);
      await load();
    } catch (e) {
      setError(humanError(e));
    } finally {
      setLoading(false);
    }
  };

  const remove = async () => {
    setSuccess(null);
    setError(null);
    setLoading(true);
    try {
      const res = await api.deleteApiKey();
      setKey("");
      setStatus(null);
      setLast4(null);
      setSuccess(res.message);
      await load();
    } catch (e) {
      setError(humanError(e));
    } finally {
      setLoading(false);
    }
  };

  if (disabled) {
    return (
      <div className="text-sm text-[var(--fg-muted)]">
        Управление ключом выключено на сервере (не задан ключ шифрования).
        Обратитесь к администратору.
      </div>
    );
  }

  const hasKey = Boolean(status && last4);
  // Prefer the backend's answer (whitelist-aware); fall back to the JWT prop
  // only until the GET resolves.
  const effectivePrivileged = serverPrivileged ?? privileged;

  return (
    <div className="space-y-4">
      {/* Role-dependent explainer */}
      <p className="text-sm text-[var(--fg-muted)]">
        {effectivePrivileged
          ? "Необязательно — по умолчанию подписка сервиса; задайте, чтобы платить своим ключом."
          : "Без ключа отправка сообщений недоступна."}
      </p>

      {/* Current status */}
      <div className="rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] p-4 text-sm">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[var(--fg-muted)]">Текущий ключ</span>
          {loading && !hasKey ? (
            <span className="text-[var(--fg-muted)]">Загрузка…</span>
          ) : hasKey ? (
            <span className="flex items-center gap-2">
              <span className="font-mono text-[var(--fg-primary)]">
                sk-…{last4}
              </span>
              <span
                className={`rounded-full px-2 py-0.5 text-[11px] ${
                  status === "active"
                    ? "bg-emerald-900/30 text-emerald-300"
                    : status === "needs_reentry"
                      ? "bg-red-900/30 text-red-300"
                      : "bg-amber-900/30 text-amber-300"
                }`}
              >
                {status === "active"
                  ? "активен"
                  : status === "needs_reentry"
                    ? "нужен повторный ввод"
                    : "не проверен"}
              </span>
            </span>
          ) : (
            <span className="text-[var(--fg-primary)]">не задан</span>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-700/40 bg-red-900/20 px-3 py-2 text-sm text-red-300">
          {error}
        </div>
      )}
      {success && (
        <div className="rounded-xl border border-emerald-700/40 bg-emerald-900/20 px-3 py-2 text-sm text-emerald-300">
          {success}
        </div>
      )}

      {/* New key input */}
      <div className="space-y-2">
        <label
          htmlFor="apikey-input"
          className="block text-[12px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]"
        >
          {hasKey ? "Заменить ключ" : "Новый ключ"}
        </label>
        <input
          id="apikey-input"
          type="password"
          autoComplete="off"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          placeholder="sk-ant-…"
          disabled={loading}
          className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-input)] px-3 py-2 font-mono text-sm text-[var(--fg-primary)] focus:outline-none disabled:opacity-50"
        />
        <div className="flex gap-2">
          <button
            onClick={() => void save()}
            disabled={loading || !key.trim()}
            className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--bg-canvas)] disabled:opacity-50"
          >
            Сохранить
          </button>
          {hasKey && (
            <button
              onClick={() => void remove()}
              disabled={loading}
              className="rounded-lg px-4 py-2 text-sm text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)] disabled:opacity-50"
            >
              Удалить
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
