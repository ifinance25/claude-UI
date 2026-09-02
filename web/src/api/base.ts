// Базовый путь приложения. Vite подставляет import.meta.env.BASE_URL из build-time
// `base`: "/" (режим домена/корня) или "/agent/" (режим по IP). Caddy в IP-режиме
// срезает /agent на бэке, но БРАУЗЕР обязан слать URL с префиксом — иначе ассеты,
// API и WS уйдут не на тот путь.

const RAW_BASE = import.meta.env.BASE_URL || "/";

// Без хвостового слэша — для конкатенации с путями вида "/api/x".
export const BASE_NO_SLASH = RAW_BASE.replace(/\/+$/, "");

// С хвостовым слэшем — для WS-URL.
export const BASE_WITH_SLASH = RAW_BASE.endsWith("/") ? RAW_BASE : `${RAW_BASE}/`;

// basename для react-router ("/agent" или "/").
export const ROUTER_BASENAME = BASE_NO_SLASH || "/";

// "/api/x" -> "/agent/api/x" (IP) либо "/api/x" (корень).
export function apiUrl(path: string): string {
  return `${BASE_NO_SLASH}${path}`;
}
