// Theme handling. Persist in localStorage; `dark` class on <html>.
// Default is light (Claude.ai parchment).

export type Theme = "dark" | "light";

const STORAGE_KEY = "ai-panel-theme";
const LEGACY_KEY = "vels-theme";

export function getStoredTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const raw = window.localStorage.getItem(STORAGE_KEY) ?? window.localStorage.getItem(LEGACY_KEY);
  return raw === "dark" ? "dark" : "light";
}

export function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("dark", theme === "dark");
  document.documentElement.classList.remove("light");
}

export function setStoredTheme(theme: Theme): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

applyTheme(getStoredTheme());
