# AI-Panel

Claude Code на вашем сервере, доступный через браузер. Пишете как в обычном
чате: он читает и правит файлы проекта, запускает команды, отвечает на вопросы.
Компьютер держать включённым не нужно: работает сервер.

В панели: несколько проектов, админка, файловый менеджер, артефакты, участники
и доступы. Telegram-бот в комплекте, но **не обязателен**: при установке его
можно пропустить и подключить позже.


---

## Что нужно

- **VPS** на Ubuntu 22.04/24.04 с доступом по `sudo` и публичным IP, минимум 2 ГБ RAM.
- **Доступ к Claude** — API-ключ с [console.anthropic.com/keys](https://console.anthropic.com/keys)
  (оплата по факту, без браузера) **или** подписка Pro/Max (один раз войти на сервере).
  Сам Claude Code CLI ставит установщик.
- **Открытый порт 80** (и 443 для домена) в security group провайдера.
- *(по желанию)* токен бота от [@BotFather](https://t.me/BotFather) и ваш
  числовой ID от [@userinfobot](https://t.me/userinfobot).

---

## Установка одной командой

```bash
curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash
```

Установщик задаст три вопроса — домен, ключ Claude, токен Telegram-бота — и
любой можно пропустить клавишей Enter. В конце покажет адрес и пароль
администратора (пароль показывается один раз).

Без вопросов, только веб:

```bash
curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo \
  ANTHROPIC_API_KEY=sk-ant-ВАШ_КЛЮЧ \
  bash
```

Со своим доменом и HTTPS (A-запись домена должна указывать на IP сервера):

```bash
curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo \
  DOMAIN=claude.ваш-домен.ru \
  ANTHROPIC_API_KEY=sk-ant-ВАШ_КЛЮЧ \
  bash
```

Подробности, варианты и разбор проблем — [docs/УСТАНОВКА.md](docs/УСТАНОВКА.md).

---

## Первый вход

1. Откройте адрес из финального сообщения: `http://<ip>/agent` или `https://ваш-домен`.
2. Логин `admin`, пароль из финала.
3. Положите проект на сервер отдельной подпапкой:

   ```bash
   sudo -u vels-bot git clone https://github.com/вы/ваш-проект.git \
     /var/lib/vels-bot/projects/ваш-проект
   ```

   Перезапуск не нужен: проект появится в списке. Новые проекты также создаются
   в админке по имени (путь = `PROJECTS_DIR` + имя).
4. Напишите Claude любое сообщение.

---

## Что в интерфейсе

- **Чат** с историей и стримингом ответа, кнопка **Стоп**.
- **Список чатов** слева с поиском, папками и новым чатом.
- **Админка** — пользователи, проекты, доступы.
- **Файлы и артефакты**, участники проекта.
- **Выбор модели** рядом с полем ввода.
- **Вложения**, документация, счётчики токенов и контекста.

Как этим пользоваться — [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Telegram (по желанию)

Бот ведёт диалоги в **форум-группе** — в личной переписке темы не работают, это
ограничение Telegram.

Подключить сразу — передайте при установке:

```bash
  TELEGRAM_BOT_TOKEN=123456:ВАШ_ТОКЕН \
  ALLOWED_USER_IDS=ВАШ_TELEGRAM_ID \
```

Подключить позже — повторите команду установки и введите токен, когда
установщик спросит: он подхватит прежние настройки, переключит сервис в режим
«бот + веб» и перезапустит его. Чаты, пароль и проект остаются на месте.

Со стороны Telegram: создайте группу → включите **«Темы» (Topics)** → добавьте
бота **админом** с правом **«Управление темами»** → создайте тему задачи.

---

## Обновление и удаление

```bash
# обновление — та же команда установки, данные не трогаются
curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash

# удаление (папка проектов и авторизация Claude сохраняются)
sudo bash /opt/vels-claude/scripts/uninstall.sh
```

---

## Конфигурация

`.env` в `/opt/vels-claude`:

| Переменная | Описание | По умолчанию |
|---|---|---|
| `PROJECTS_DIR` | Папка, внутри которой лежит проект | `/var/lib/vels-bot/projects` |
| `ADMIN_LOGIN` / `ADMIN_PASSWORD` | Вход в веб | `admin` / генерируется |
| `WEB_JWT_SECRET` | Подпись сессионных cookie | генерируется |
| `ANTHROPIC_API_KEY` | Ключ Claude (или `CLAUDE_CODE_OAUTH_TOKEN`) | — |
| `SESSION_DATABASE_PATH` | SQLite с чатами | `data/sessions.db` |
| `TELEGRAM_BOT_TOKEN` | Токен бота — пусто, если бот не нужен | — |
| `ALLOWED_USER_IDS` | Telegram ID через запятую | — |

Остальное — `config/config.yaml` (модель, таймауты, режим разрешений, порт
веба). После правки: `systemctl restart vels-claude`.

## Управление сервисом

```bash
systemctl status vels-claude
journalctl -u vels-claude -f
systemctl restart vels-claude
```

---

## Запуск локально (без установщика)

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # Windows: .venv/Scripts/python
cd web && npm install && npm run build && cd ..
cp .env.example .env        # заполнить PROJECTS_DIR, WEB_JWT_SECRET, ADMIN_LOGIN, ADMIN_PASSWORD
python scripts/run_web.py            # только веб, без Telegram
python scripts/run_web.py --port 8600   # если порт из конфига занят
```

`python -m src.main` поднимает бота вместе с вебом и требует токен Telegram.

Тесты:

```bash
python -m pytest tests -q
cd web && npx vitest run
```

---

## Устранение неполадок

| Симптом | Что делать |
|---|---|
| Адрес не открывается снаружи | Откройте порт 80 (и 443 для домена) в security group провайдера |
| Чат не отвечает, Claude не авторизован | `echo 'ANTHROPIC_API_KEY=sk-ant-...' \| sudo tee -a /opt/vels-claude/.env` и `systemctl restart vels-claude` |
| Claude Code CLI не найден | `npm install -g @anthropic-ai/claude-code`, проверить `claude --version`, повторить установку |
| Проект не виден | Проверьте `PROJECTS_DIR` в `.env`; проект должен лежать **подпапкой** внутри неё |
| Виден не тот проект | Выберите нужный в сайдбаре или в админке; проверьте `PROJECTS_DIR` |
| Бот не отвечает, веб работает | Ваш ID в `ALLOWED_USER_IDS`, бот — админ форум-группы с правом «Управление темами» |
| Сервис не стартует | `systemctl status vels-claude` и `journalctl -u vels-claude -n 50 --no-pager` |
