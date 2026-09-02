/// <reference types="vite/client" />

// Собственных VITE_*-переменных у фронта нет: базовый путь Vite подставляет
// сам (import.meta.env.BASE_URL из VITE_BASE), а имя Telegram-бота страница
// входа спрашивает у сервера — /api/auth/config, а не build-time env.
interface ImportMetaEnv {}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
