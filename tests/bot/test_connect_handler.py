from src.bot.handlers.connect import ConnectStates, parse_connect_data, service_help_text


def test_states_group_has_awaiting_token():
    assert hasattr(ConnectStates, "awaiting_token")


def test_parse_connect_data():
    assert parse_connect_data("connect:pick:notion") == ("pick", "notion")
    assert parse_connect_data("connect:list") == ("list", "")
    assert parse_connect_data("connect:disconnect:github") == ("disconnect", "github")


def test_service_help_text_mentions_how_to():
    txt = service_help_text("notion")
    assert "Notion" in txt
    assert "notion.so" in txt.lower()
    assert service_help_text("nope") == ""
