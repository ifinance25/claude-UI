from src.bot.keyboards.models import create_model_keyboard
from src.claude import models


def test_one_button_per_known_model_in_order():
    kb = create_model_keyboard("claude-sonnet-5")
    assert len(kb.inline_keyboard) == len(models.KNOWN_MODELS)
    for row, m in zip(kb.inline_keyboard, models.KNOWN_MODELS):
        assert len(row) == 1
        assert row[0].callback_data == f"model:set:{m['id']}"


def test_check_mark_only_on_current():
    kb = create_model_keyboard("claude-opus-5")
    texts = [row[0].text for row in kb.inline_keyboard]
    checked = [t for t in texts if t.startswith("✓ ")]
    assert checked == ["✓ Claude Opus 5"]


def test_alias_current_is_normalized_for_check_mark():
    kb = create_model_keyboard("opus")  # stored alias
    texts = [row[0].text for row in kb.inline_keyboard]
    assert any(t == "✓ Claude Opus 5" for t in texts)


def test_unknown_current_has_no_check_mark():
    kb = create_model_keyboard("claude-future-9")
    texts = [row[0].text for row in kb.inline_keyboard]
    assert not any(t.startswith("✓ ") for t in texts)


def test_callback_data_within_telegram_limit():
    kb = create_model_keyboard("claude-sonnet-5")
    for row in kb.inline_keyboard:
        assert len(row[0].callback_data.encode("utf-8")) <= 64


def test_non_current_buttons_have_no_prefix():
    kb = create_model_keyboard("claude-opus-5")
    for row, m in zip(kb.inline_keyboard, models.KNOWN_MODELS):
        if m["id"] == "claude-opus-5":
            continue
        assert row[0].text == m["label"]
