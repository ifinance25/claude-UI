from src.bot import onboarding


def test_start_message_mentions_connect():
    assert "/connect" in onboarding.start_message()
