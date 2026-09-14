# AI-Panel — что это и как запустить

Claude Code на своём сервере через браузер и, по желанию, Telegram. Несколько
проектов, админка, файловый менеджер, участники и доступы.

## Что в интерфейсе

| Есть |
|---|
| Чат с Claude Code (стриминг, история, модели) |
| Список чатов, поиск, папки, диалог нового чата |
| Файловый менеджер, редактор, артефакты |
| Участники проекта и выдача доступов |
| Админка: пользователи, проекты, доступы |
| Заметки и тоггл «Размышления» |
| Документация, счётчики расхода и контекста |
| Кнопка «продолжить в Telegram» |

Telegram-бот: `python -m src.main` поднимает его вместе с вебом. Команда
`/projects` выбирает проект. Без токена бота остаётся только веб
(`scripts/run_web.py`).

## Установка на сервер

```bash
curl -fsSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash
```

Установщик спрашивает домен (Enter: доступ по IP через sslip.io, Caddy сам
выпустит TLS) и необязательный токен Telegram-бота. Логин админа `admin`,
пароль показывается один раз в конце. С токеном systemd запускает
`python -m src.main`, без токена — `scripts/run_web.py`.

## Запуск вручную, только веб

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
cd web && npm install && npm run build && cd ..
cp .env.example .env        # PROJECTS_DIR, WEB_JWT_SECRET, ADMIN_LOGIN, ADMIN_PASSWORD
python scripts/run_web.py
```

Открыть адрес из лога (по умолчанию http://127.0.0.1:8765) и войти под
`ADMIN_LOGIN` / `ADMIN_PASSWORD`.

**Порт занят?** `python scripts/run_web.py --port 8600`.

**Проект.** `PROJECTS_DIR` — папка, внутри которой лежат проекты (подпапки).
Новые проекты добавляются в админке по имени; путь = `PROJECTS_DIR` + имя.

## Проверка API без сессии

Живые эндпоинты без авторизации отдают 401, SPA-роуты — 200:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8600/api/files/tree   # 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8600/api/admin/users  # 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8600/api/docs         # 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8600/login            # 200
```

Catch-all SPA пропускает `/api/*` — несуществующий API отдаёт 404, не `index.html`.

## Тесты

```bash
.venv/Scripts/python.exe -m pytest tests -q
cd web && npx vitest run
```
