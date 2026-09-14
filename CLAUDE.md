# CLAUDE.md — инструкция по проекту

## Обзор

**AI-Panel** — Claude Code на своём сервере, доступный через браузер. Веб —
основной интерфейс; Telegram-бот идёт в комплекте, но **необязателен**: без
токена сервис поднимает только веб.

Панель рассчитана на личную работу и небольшую команду: несколько проектов,
админка, файлы, участники и доступы. Источник кода:
[ifinance25/claude-UI](https://github.com/ifinance25/claude-UI).

**Стек:** Python 3.11+, FastAPI + uvicorn, React 19 + Vite + Tailwind,
aiogram 3.26 (Telegram), Claude Agent SDK / Claude Code CLI, SQLAlchemy +
SQLite, structlog.

---

## Что есть в интерфейсе

| Что | Где |
|---|---|
| Чат с Claude Code (стриминг, история, модели) | `web/src/components/Chat.tsx`, `MessageInput.tsx` |
| Список чатов, поиск, папки, новый чат | `Sidebar.tsx`, `NewChatDialog.tsx` |
| Файловый менеджер, редактор, поиск | `src/web/routes_files.py`, `web/src/components/files/` |
| Артефакты Claude | `/api/files/artifacts`, `components/artifacts/` |
| Участники проекта и выдача доступов | `src/web/routes_members.py`, `ProjectMembersPanel.tsx` |
| Админка: пользователи, проекты, доступы | `src/web/routes_admin.py`, `AdminPage.tsx`, `components/admin/` |
| Заметки к чату и тоггл «Размышления» | панель и кнопка в `Chat.tsx` |
| Документация | `routes_docs.py`, `docs/USER-GUIDE.md` |
| Настройки, ключи API, подключения MCP | `SettingsPage.tsx`, `routes_settings.py`, `routes_connections.py` |

Заголовок чата берётся из первого сообщения (`deriveTitle` → `patchSessionNotes`).
Скачивание файлов из ленты: `/api/sessions/{uuid}/file`.

Telegram: команда `/projects` и клавиатура выбора проекта остаются; веб тоже
умеет выбирать проект. `src/bot/single_project.py` и
`Settings.get_light_project_paths()` по-прежнему отдают **первый** проект из
`PROJECTS_DIR`, если список путей не задан явно. Админка пишет проекты в БД и
раздаёт доступы поверх этого.

---

## Два входа

| Вход | Что поднимает | Когда используется |
|---|---|---|
| `scripts/run_web.py` | сессии, шина, мост к Claude, веб-сервер | Telegram не настроен (нет токена) |
| `python -m src.main` | всё то же + Telegram-бот, вебхуки, cron | токен бота задан |

`python -m src.main` **без токена не стартует** — валидирует его и выходит с
кодом 1. Установщик это учитывает: `scripts/install.sh` пишет в systemd-юнит
`run_web.py`, когда токена нет, и `-m src.main`, когда есть.

`run_web.py` принимает `--host` / `--port`.

---

## Быстрый старт локально

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
cd web && npm install && npm run build && cd ..
cp .env.example .env    # PROJECTS_DIR, WEB_JWT_SECRET, ADMIN_LOGIN, ADMIN_PASSWORD
.venv/Scripts/python scripts/run_web.py --port 8600
```

Claude Code CLI ставится отдельно (`npm install -g @anthropic-ai/claude-code`)
и должен быть авторизован — ключом `ANTHROPIC_API_KEY` или входом по подписке.

Тесты:

```bash
.venv/Scripts/python -m pytest tests -q     # бэкенд
cd web && npx vitest run                    # фронт
bash tests/installer_test.sh                # установщик
ruff check src/
```

---

## Архитектура

```
Браузер (React SPA)                      Telegram (форум-группа) — опционально
    │  REST + WebSocket                      │  aiogram
    ▼                                        ▼
src/web/  (FastAPI)                      src/bot/
    │  публикует UserMessageReceived          │
    └──────────────► src/event_bus ◄──────────┘
                          │
                          ▼
              src/event_bus/claude.py  (ClaudeEventRelay)
                          │
                          ▼
               src/claude/bridge.py  (SDK / CLI / tmux)
                          │
                          ▼
                    Claude Code
```

Веб и Telegram не знают друг о друге: оба говорят с шиной событий. Web-only
вход собирается из тех же кусков, без `src/bot`.

**Слои:**

- `src/web/` — FastAPI: роутеры (`routes_*.py`), авторизация (`auth.py`,
  `routes_auth.py`, `passwords.py`), первый админ (`bootstrap.py`), проброс
  событий в WebSocket (`ws_forwarder.py`), запись истории (`message_store.py`),
  раздача собранного фронта (`mount_frontend` в `server.py`).
- `src/claude/` — мост к Claude Code (`bridge.py`), сессии и БД (`session.py`),
  нативные команды, файрвол инструментов, скиллы, модели.
- `src/event_bus/` — шина событий и релей запросов к Claude.
- `src/bot/` — Telegram: ядро, хендлеры, онбординг, выбор проекта.
- `src/connections/`, `src/apikeys/` — per-user MCP-подключения и Anthropic-ключи.
- `web/src/` — фронт: чат и шапка, сайдбар, ввод, админка, файлы, маршруты
  Login / Chat / Settings / Admin.

---

## Streaming

`claude -p "<текст>" --output-format stream-json --include-partial-messages
--resume <session_id>` отдаёт JSON-события построчно:

| Событие | Обработка |
|---|---|
| `system` (init) | запоминаем `session_id` |
| `stream_event` / `text_delta` | токены → в буфер и в WebSocket |
| `stream_event` / `content_block_*` | игнорируем |
| `assistant` | **игнорируем** — дублирует `text_delta` |
| `result` | финал, usage, стоимость |

В вебе дельты уходят по WebSocket и склеиваются в один пузырь; в Telegram —
через `sendMessageDraft` (обновление каждые 30 мс, без rate-limit).

---

## Команды

**Нативные** — Claude Code понимает их только в интерактивном терминале,
поэтому их обрабатывает сервис: `/model`, `/model sonnet|opus`, `/config`,
`/mcp`, `/permissions`.

**Проброс в Claude:** `/cost`, `/context`, `/compact`, `/init`, `/review`,
`/diff`, `/clear`, `/security-review` и прочие — список зависит от проекта.

**Команды бота** (только когда Telegram подключён): `/start`, `/status`,
`/close`, `/stop`, `/help`, `/projects`.

---

## Конфигурация

`.env` (секреты, не в git):

| Переменная | Назначение |
|---|---|
| `PROJECTS_DIR` | папка, **внутри** которой лежат проекты (сканируются подпапки) |
| `ADMIN_LOGIN` / `ADMIN_PASSWORD` | первый админ; заводится при старте, если такого логина нет |
| `WEB_JWT_SECRET` | подпись cookie сессии (`vels_session`) |
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` | авторизация Claude |
| `SESSION_DATABASE_PATH` | SQLite с чатами (`data/sessions.db`) |
| `CONNECTIONS_SECRET_KEY` | Fernet-ключ для секретов Connect Services; без него фича спит |
| `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` | Telegram; пусто → веб-only |

`config/config.yaml` — модель, транспорт (`sdk`/`cli`/`tmux`), режим
разрешений, таймауты, `web.enabled` (по умолчанию `true`), host/port.
После правки: `systemctl restart vels-claude`.

Имена systemd-юнита, пользователя `vels-bot` и каталога `/opt/vels-claude`
оставлены как есть: так стоит прод. Менять их только вместе с установщиком и
уже работающим сервером.

---

## Развёртывание

`scripts/install.sh` — production-установщик под Ubuntu/Debian: ставит
зависимости и Claude Code CLI, создаёт сервис-юзера, клонирует этот репозиторий,
собирает фронт, пишет `.env`, поднимает Caddy с TLS (домен или sslip.io),
ставит systemd-юнит, открывает firewall, проверяет доступность снаружи.

Спрашивает три вещи, каждую можно пропустить: домен, ключ Claude, токен бота.
Идемпотентен — повторный запуск обновляет установку и переписывает юнит
(так же подключается Telegram к уже работающей инсталляции).

`scripts/make-release.sh` собирает публичный архив через `git archive`.
Состав архива определяется `.gitattributes` (`export-ignore`): внутренние
доки и тесты не уезжают, а `docs/USER-GUIDE.md` уезжает — панель
«Документация» читает его с диска в рантайме. Регресс закрыт тестом в `tests/test_installer.py`.

---

## Известные особенности

1. Первый чанк от Claude приходит большим (300–500 символов) — так работает API.
2. `assistant`-событие дублирует `text_delta` и намеренно игнорируется.
3. `session_id` хранится на сессию; `--resume` восстанавливает контекст.
4. Прокси-переменные вычищаются из окружения перед запуском CLI — VPN должен
   работать на уровне системы.
5. `verbose_level` (0–3) ветвится только в `src/bot/` — на веб-поток он не
   влияет.
6. Catch-all SPA не перехватывает `/api/*`: несуществующий эндпоинт отдаёт 404,
   а не `index.html` (иначе фронт получал 200 с HTML вместо ошибки).
7. Telegram-темы — функция супергрупп: в личном чате `createForumTopic`
   возвращает `the chat is not a forum`.

---

## MCP: нативный против «Подключений»

- **Нативный MCP** (`claude mcp add`, project `.mcp.json`,
  `settings.json:mcpServers`) — только для owner-сессий. Установщик ставит
  `enableAllProjectMcpServers: true` в `settings.json` сервис-юзера, иначе
  headless-Claude не подхватывает project-scope MCP.
- **Per-user «Подключения»** передаются в сессию явно и изолированно; в
  джейл монтируется санитизированная копия `settings.json` без MCP-ключей —
  иначе вредоносный `.mcp.json` в проекте стартовал бы subprocess до
  PreToolUse-файрвола.

Второй путь включается `CONNECTIONS_SECRET_KEY`.
