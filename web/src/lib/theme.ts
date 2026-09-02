// Theme handling. The chosen theme is persisted in localStorage and
// applied by toggling the `light` class on <html>. Default is dark.

export type Theme = "dark" | "light";

const STORAGE_KEY = "vels-theme";

export function getStoredTheme(): Theme {
  if (typeof window === "undefined") return "dark";
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return raw === "light" ? "light" : "dark";
}

export function applyTheme(theme: Theme): void {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("light", theme === "light");
}

export function setStoredTheme(theme: Theme): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, theme);
  applyTheme(theme);
}

// Apply the persisted theme on first load so the page doesn't flash dark
// before React mounts. Safe to import multiple times.
applyTheme(getStoredTheme());
