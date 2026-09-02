import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ConnectionService } from "@/lib/types";

export default function ConnectionsPanel() {
  const [enabled, setEnabled] = useState(true);
  const [services, setServices] = useState<ConnectionService[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const info = await api.listConnections();
      setEnabled(info.enabled);
      setServices(info.services);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  useEffect(() => { void load(); }, []);

  const connect = async (id: string) => {
    if (!secret.trim()) return;
    setBusy(true);
    try {
      await api.connectService(id, secret.trim());
      setSecret(""); setOpenId(null);
      await load();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const disconnect = async (id: string) => {
    setBusy(true);
    try { await api.disconnectService(id); await load(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  if (!enabled) {
    return (
      <div className="text-sm text-[var(--fg-muted)]">
        Подключения выключены на сервере (не задан ключ шифрования). Обратитесь к администратору.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {error && <div className="rounded-lg bg-red-900/30 px-3 py-2 text-sm text-red-300">{error}</div>}
      {services.map((s) => (
        <div key={s.id} className="rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] p-3">
          <div className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="text-sm font-semibold text-[var(--fg-primary)]">
                <span className="mr-1.5">{s.icon}</span>
                <span>{s.name}</span>
              </div>
              <div className="truncate text-xs text-[var(--fg-muted)]">{s.description}</div>
            </div>
            {s.connected ? (
              <button
                onClick={() => void disconnect(s.id)}
                disabled={busy}
                className="shrink-0 rounded-lg px-3 py-1.5 text-sm text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
              >Отключить</button>
            ) : (
              <button
                onClick={() => { setOpenId(openId === s.id ? null : s.id); setSecret(""); }}
                disabled={busy}
                className="shrink-0 rounded-lg bg-[var(--bg-hover)] px-3 py-1.5 text-sm text-[var(--fg-primary)]"
              >Подключить</button>
            )}
          </div>
          {openId === s.id && !s.connected && (
            <div className="mt-3 space-y-2 border-t border-[var(--border-subtle)] pt-3">
              <ol className="list-decimal pl-5 text-xs text-[var(--fg-secondary)]">
                {s.how_to_steps.map((step, i) => <li key={i}>{step}</li>)}
              </ol>
              <a href={s.how_to_url} target="_blank" rel="noreferrer" className="text-xs text-[#6ea8fe] underline">
                {s.how_to_url}
              </a>
              <input
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder={s.secret_label}
                className="w-full rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-input)] px-3 py-2 text-sm text-[var(--fg-primary)] focus:outline-none"
              />
              <button
                onClick={() => void connect(s.id)}
                disabled={busy || !secret.trim()}
                className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-[var(--bg-canvas)] disabled:opacity-50"
              >Сохранить</button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
