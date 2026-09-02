"""L-4: окружение подпроцесса Claude чистится не только по фиксированному
списку, но и эвристически — секретообразные имена удаляются, нужные Claude
ключи сохраняются, рабочее окружение (PATH) остаётся."""
import os

from src.claude.bridge import ClaudeBridge


def test_clean_env_scrubs_known_and_future_secrets(monkeypatch) -> None:
    monkeypatch.setenv("WEB_JWT_SECRET", "s")          # известный денилист
    monkeypatch.setenv("SOME_FUTURE_TOKEN", "t")       # будущий секрет (_TOKEN$)
    monkeypatch.setenv("DB_PASSWORD", "p")             # PASSWORD
    monkeypatch.setenv("STRIPE_API_KEY", "k")          # _API_KEY$
    monkeypatch.setenv("MY_BEARER", "b")               # BEARER
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")     # нужен Claude — сохраняем
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "o")  # нужен Claude — сохраняем

    env = ClaudeBridge._clean_env()

    assert "WEB_JWT_SECRET" not in env
    assert "SOME_FUTURE_TOKEN" not in env
    assert "DB_PASSWORD" not in env
    assert "STRIPE_API_KEY" not in env
    assert "MY_BEARER" not in env
    # Keep-list: авторизация Claude не должна сломаться.
    assert env.get("ANTHROPIC_API_KEY") == "key"
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "o"
    # Рабочее окружение (Bash) сохраняется (PATH не вычищается).
    assert "PATH" in env or "Path" in env


def test_confined_env_strips_anthropic_when_subscription_present(monkeypatch):
    from src.claude.bridge import ClaudeBridge
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    monkeypatch.setattr(ClaudeBridge, "_subscription_creds_present", staticmethod(lambda: True))
    env = ClaudeBridge._clean_env(confined=True)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_confined_env_keeps_anthropic_when_no_subscription(monkeypatch):
    from src.claude.bridge import ClaudeBridge
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setattr(ClaudeBridge, "_subscription_creds_present", staticmethod(lambda: False))
    env = ClaudeBridge._clean_env(confined=True)
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-secret"


def test_nonconfined_env_keeps_anthropic(monkeypatch):
    from src.claude.bridge import ClaudeBridge
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setattr(ClaudeBridge, "_subscription_creds_present", staticmethod(lambda: True))
    env = ClaudeBridge._clean_env(confined=False)
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-secret"
