"""SP2 Task 5 — инъекция пользовательского ANTHROPIC_API_KEY в _spawn_env.

Режим USER_KEY: у confined-сессии есть собственный ключ пользователя. Он
подставляется в env подпроцесса Claude ПОСЛЕ _clean_env — иначе confined-strip
вычистил бы ANTHROPIC_API_KEY (он в _SECRET_KEEP_KEYS и режется при confined +
наличии ~/.claude). Плюс выставляется VELS_JAIL_NO_OWNER_CREDS=1 — сигнал
vels-claude-jail.sh не бинд-маунтить owner-креды (сессия платит своим ключом).
"""
import inspect

from src.claude.bridge import ClaudeBridge


def _bridge() -> ClaudeBridge:
    return ClaudeBridge(permission_mode="bypassPermissions")


def test_spawn_env_injects_api_key_when_provided() -> None:
    """Ключ пользователя попадает в ANTHROPIC_API_KEY окружения подпроцесса."""
    env = _bridge()._spawn_env(anthropic_api_key="sk-ant-user-123")
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-user-123"


def test_spawn_env_sets_no_owner_creds_signal_when_key_provided() -> None:
    """Вместе с ключом выставляется сигнал джейлу не монтировать owner-креды."""
    env = _bridge()._spawn_env(anthropic_api_key="sk-ant-user-123")
    assert env["VELS_JAIL_NO_OWNER_CREDS"] == "1"


def test_injected_key_survives_confined_clean_env_strip(monkeypatch) -> None:
    """Ключевой инвариант: инъекция ПОСЛЕ _clean_env. Даже когда confined-strip
    вырезает owner-ключ из окружения (subscription present), пользовательский
    ключ переживает и подменяет его — а не наоборот."""
    # У процесса-родителя (systemd/.env) есть owner-ключ, который confined-strip
    # обязан вырезать при наличии ~/.claude.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-OWNER-should-be-stripped")
    monkeypatch.setattr(
        ClaudeBridge, "_subscription_creds_present", staticmethod(lambda: True)
    )

    env = _bridge()._spawn_env(
        confined=True,
        confine_root="/srv/project",
        anthropic_api_key="sk-ant-USER-survives",
    )

    # Именно пользовательский ключ, а не owner-ключ родителя.
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-USER-survives"
    assert env["VELS_JAIL_NO_OWNER_CREDS"] == "1"


def test_spawn_env_no_injection_without_key(monkeypatch) -> None:
    """Без ключа: сигнал джейлу не выставляется, а confined-strip продолжает
    удалять owner-ключ (обратная совместимость — прежнее поведение)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-owner")
    monkeypatch.delenv("VELS_JAIL_NO_OWNER_CREDS", raising=False)
    monkeypatch.setattr(
        ClaudeBridge, "_subscription_creds_present", staticmethod(lambda: True)
    )

    env = _bridge()._spawn_env(confined=True, confine_root="/srv/project")

    assert "VELS_JAIL_NO_OWNER_CREDS" not in env
    # Без пользовательского ключа owner-ключ вычищается как раньше.
    assert "ANTHROPIC_API_KEY" not in env


def test_spawn_env_injection_isolated_from_clean_env(monkeypatch) -> None:
    """Инъекция не зависит от содержимого _clean_env: даже если _clean_env
    вернул пустой словарь, ключ и сигнал добавляются поверх."""
    monkeypatch.setattr(
        ClaudeBridge, "_clean_env", staticmethod(lambda confined=False: {})
    )

    env = _bridge()._spawn_env(anthropic_api_key="sk-ant-injected")

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-injected"
    assert env["VELS_JAIL_NO_OWNER_CREDS"] == "1"


def test_empty_string_key_is_not_injected(monkeypatch) -> None:
    """Пустая строка/None трактуется как «ключа нет» (truthiness) — не инъектим
    пустой ANTHROPIC_API_KEY и не сигналим джейлу."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VELS_JAIL_NO_OWNER_CREDS", raising=False)

    env = _bridge()._spawn_env(anthropic_api_key="")

    assert "VELS_JAIL_NO_OWNER_CREDS" not in env
    assert "ANTHROPIC_API_KEY" not in env


class _DummyOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_build_sdk_options_threads_api_key_into_env() -> None:
    """send_message → _build_sdk_options → _spawn_env: ключ доходит до env
    опций SDK (это и есть путь, по которому подпроцесс наследует окружение)."""
    opt = _bridge()._build_sdk_options(
        options_cls=_DummyOptions,
        project_path="/x",
        session_id=None,
        anthropic_api_key="sk-ant-threaded",
    )
    assert opt.kwargs["env"]["ANTHROPIC_API_KEY"] == "sk-ant-threaded"
    assert opt.kwargs["env"]["VELS_JAIL_NO_OWNER_CREDS"] == "1"


def test_send_message_accepts_anthropic_api_key_param() -> None:
    """Публичный контракт: у send_message есть параметр anthropic_api_key
    (по умолчанию None — обратная совместимость)."""
    sig = inspect.signature(ClaudeBridge.send_message)
    assert "anthropic_api_key" in sig.parameters
    assert sig.parameters["anthropic_api_key"].default is None
