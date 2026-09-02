from src.bot.keyboards.connections import (
    create_connect_catalog_keyboard,
    create_my_connections_keyboard,
)


def test_catalog_has_a_button_per_service():
    kb = create_connect_catalog_keyboard()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "connect:pick:notion" in datas
    assert "connect:pick:github" in datas


def test_my_connections_shows_disconnect_for_connected():
    kb = create_my_connections_keyboard(["notion"])
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "connect:disconnect:notion" in datas


def test_my_connections_empty():
    kb = create_my_connections_keyboard([])
    assert kb.inline_keyboard is not None
