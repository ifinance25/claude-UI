"""Unit tests for the single-source model registry."""
from src.claude import models


def test_valid_model_ids_matches_known():
    ids = models.valid_model_ids()
    assert ids == {m["id"] for m in models.KNOWN_MODELS}
    assert "claude-opus-5" in ids
    assert "claude-fable-5-1" in ids


def test_normalize_alias_to_pinned_id():
    assert models.normalize_model_id("fable") == "claude-fable-5-1"
    assert models.normalize_model_id("opus") == "claude-opus-5"
    assert models.normalize_model_id("sonnet") == "claude-sonnet-5"
    assert models.normalize_model_id("haiku") == "claude-haiku-4-5-20251001"


def test_normalize_pinned_id_unchanged():
    assert models.normalize_model_id("claude-opus-5") == "claude-opus-5"


def test_normalize_unknown_returned_as_is():
    assert models.normalize_model_id("claude-future-9") == "claude-future-9"


def test_resolve_input_alias():
    assert models.resolve_model_input("opus") == "claude-opus-5"


def test_resolve_input_known_id():
    assert models.resolve_model_input("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"


def test_resolve_input_strips_whitespace_edges():
    assert models.resolve_model_input("  opus  ") == "claude-opus-5"


def test_resolve_input_unknown_is_none():
    assert models.resolve_model_input("gpt-4") is None


def test_resolve_input_rejects_empty_and_spacey_and_long():
    assert models.resolve_model_input("") is None
    assert models.resolve_model_input("   ") is None
    assert models.resolve_model_input("op us") is None
    assert models.resolve_model_input("x" * 200) is None


def test_model_label_known_and_unknown():
    assert models.model_label("claude-opus-5") == "Claude Opus 5"
    # Незнакомая модель больше НЕ показывается сырым id: модели выходят чаще,
    # чем правится KNOWN_MODELS, и в интерфейсе появлялось «claude-fable-5-1».
    assert models.model_label("claude-future-9") == "Claude Future 9"


def test_humanize_model_id():
    """Читаемое имя без сети — на случай, когда живой каталог недоступен."""
    assert models.humanize_model_id("claude-fable-5-1") == "Claude Fable 5.1"
    assert models.humanize_model_id("claude-opus-5") == "Claude Opus 5"
    # Снапшотные хвосты — это версионирование, а не часть имени.
    assert models.humanize_model_id("claude-haiku-4-5-20251001") == "Claude Haiku 4.5"
    assert models.humanize_model_id("claude-opus-4-6-20251101-v1") == "Claude Opus 4.6"
    # Режим в хвосте несёт смысл и остаётся в подписи.
    assert models.humanize_model_id("claude-opus-4-6-fast") == "Claude Opus 4.6 Fast"
    # Чужой формат не трогаем и пустую строку не выдумываем.
    assert models.humanize_model_id("gpt-4") == "gpt-4"
    assert models.humanize_model_id("") == ""


def test_is_safe_model_id():
    """Allowlist моделей живой, поэтому формат id проверяется строго: значение
    пишется дословно в общий ~/.claude/settings.json."""
    assert models.is_safe_model_id("claude-fable-5-1")
    assert models.is_safe_model_id("claude-haiku-4-5-20251001")
    assert not models.is_safe_model_id("")
    assert not models.is_safe_model_id("../../etc/passwd")
    assert not models.is_safe_model_id("claude opus 5")
    assert not models.is_safe_model_id("Claude-Opus-5")
    assert not models.is_safe_model_id("c" * 129)


def test_model_hint_known_and_unknown():
    assert "силь" in models.model_hint("claude-opus-5").lower()
    assert models.model_hint("claude-future-9") == ""
