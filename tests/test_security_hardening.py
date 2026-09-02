"""Юнит-тесты мер безопасности, добавленных в аудит 2026-06-22:
- проверка Origin (CSWSH/CSRF),
- rate-limiter эндпоинтов входа,
- санитизация имён Telegram-загрузок (path traversal),
- вычистка секретов бота из окружения подпроцесса Claude.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.web.origin_check import build_allowed_origins, is_allowed_origin
from src.web.rate_limit import LoginRateLimiter, client_key


# --- Origin / CSWSH / CSRF --------------------------------------------------

def test_origin_missing_is_allowed():
    # Не-браузерный клиент (curl/TestClient) — не CSRF-вектор.
    assert is_allowed_origin(None, "host", set()) is True


def test_origin_same_host_allowed():
    assert is_allowed_origin("https://app.example.com", "app.example.com", set()) is True


def test_origin_cross_site_blocked():
    assert is_allowed_origin("https://evil.com", "app.example.com", set()) is False


def test_origin_allowlist_match():
    allowed = build_allowed_origins("https://app.example.com")
    assert is_allowed_origin("https://app.example.com", "other-host", allowed) is True


def test_origin_loopback_allowed_for_dev():
    assert is_allowed_origin("http://localhost:5173", "127.0.0.1:8765", set()) is True
    assert is_allowed_origin("http://127.0.0.1:5173", "x", set()) is True


def test_build_allowed_origins_parses_scheme_host():
    assert build_allowed_origins("https://a.b/c") == {"https://a.b"}
    assert build_allowed_origins("") == set()
    assert build_allowed_origins(None) == set()


# --- Rate limiter -----------------------------------------------------------

def test_rate_limiter_blocks_after_threshold():
    rl = LoginRateLimiter(max_fails=3, window_seconds=1000, lockout_seconds=1000)
    key = "1.2.3.4|bob"
    assert not rl.is_blocked(key)
    for _ in range(3):
        rl.record_failure(key)
    assert rl.is_blocked(key)
    assert rl.retry_after(key) > 0


def test_rate_limiter_reset_on_success():
    rl = LoginRateLimiter(max_fails=2, window_seconds=1000, lockout_seconds=1000)
    key = "1.2.3.4|bob"
    rl.record_failure(key)
    rl.record_failure(key)
    assert rl.is_blocked(key)
    rl.reset(key)
    assert not rl.is_blocked(key)


# --- Telegram upload filename sanitization (path traversal) -----------------

def test_safe_upload_name_strips_traversal():
    from src.bot.handlers.files import _safe_upload_name

    assert _safe_upload_name("../../../root/.ssh/authorized_keys") == "authorized_keys"
    assert _safe_upload_name("/etc/passwd") == "passwd"
    assert _safe_upload_name("..") == "upload"  # дефолтный fallback
    assert _safe_upload_name("..", "fb") == "fb"
    assert "/" not in _safe_upload_name("a/b/c.txt")
    assert "\\" not in _safe_upload_name("a\\b\\c.txt")
    # Нормальное имя сохраняется.
    assert _safe_upload_name("report.pdf") == "report.pdf"


# --- Claude subprocess env scrubbing ----------------------------------------

def test_clean_env_drops_bot_secrets(monkeypatch):
    from src.claude.bridge import ClaudeBridge

    monkeypatch.setenv("WEB_JWT_SECRET", "supersecret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-keep")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = ClaudeBridge._clean_env()

    assert "WEB_JWT_SECRET" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "ADMIN_PASSWORD" not in env
    # Claude нужен ключ авторизации — он остаётся.
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-keep"
    assert env.get("PATH") == "/usr/bin"


# --- BD1: client_key анти-спуфинг X-Forwarded-For --------------------------


def _req(client_host, xff=None):
    h = {}
    if xff is not None:
        h["x-forwarded-for"] = xff
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    return SimpleNamespace(headers=h, client=client)


def test_client_key_trusts_loopback_xff_rightmost():
    # За локальным прокси берём ПОСЛЕДНИЙ адрес (реальный клиент), не первый.
    assert client_key(_req("127.0.0.1", "1.2.3.4, 9.9.9.9"), "bob") == "9.9.9.9|bob"


def test_client_key_ignores_xff_from_untrusted_client():
    # Прямое подключение (не loopback): XFF игнорируется → адрес соединения.
    assert client_key(_req("203.0.113.5", "1.2.3.4"), "bob") == "203.0.113.5|bob"


def test_client_key_spoofing_first_element_does_not_change_bucket():
    # Рандомизация первого элемента XFF не меняет ключ (берём правый хоп).
    k1 = client_key(_req("127.0.0.1", "11.11.11.11, 9.9.9.9"), "bob")
    k2 = client_key(_req("127.0.0.1", "22.22.22.22, 9.9.9.9"), "bob")
    assert k1 == k2 == "9.9.9.9|bob"


def test_client_key_no_xff_uses_client_host():
    assert client_key(_req("127.0.0.1"), "bob") == "127.0.0.1|bob"
