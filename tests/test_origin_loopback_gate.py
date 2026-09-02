"""L-6: loopback-Origin разрешён только когда allow_loopback=True (dev).
В проде (allow_loopback=False) localhost/127.0.0.1 как cross-site Origin
отклоняются."""
from src.web.origin_check import is_allowed_origin


def test_loopback_allowed_in_dev() -> None:
    assert is_allowed_origin(
        "http://localhost:5173", "agent.example.com", set(), allow_loopback=True
    ) is True


def test_loopback_rejected_in_prod() -> None:
    assert is_allowed_origin(
        "http://localhost:5173", "agent.example.com", set(), allow_loopback=False
    ) is False
    assert is_allowed_origin(
        "http://127.0.0.1", "agent.example.com", set(), allow_loopback=False
    ) is False


def test_same_origin_and_allowlist_still_pass_in_prod() -> None:
    # Same-host и явный allowlist проходят независимо от loopback-флага.
    assert is_allowed_origin(
        "https://agent.example.com", "agent.example.com", set(), allow_loopback=False
    ) is True
    assert is_allowed_origin(
        "https://app.example.com",
        "agent.example.com",
        {"https://app.example.com"},
        allow_loopback=False,
    ) is True


def test_missing_origin_still_passes() -> None:
    # Не-браузерный клиент без Origin — не CSRF-вектор (дизайн сохранён).
    assert is_allowed_origin(None, "agent.example.com", set(), allow_loopback=False) is True
