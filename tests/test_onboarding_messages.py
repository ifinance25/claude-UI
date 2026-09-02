from __future__ import annotations

import unittest
from pathlib import Path

from src.bot import onboarding


class OnboardingMessageTests(unittest.TestCase):
    def test_start_message_uses_vels_brand_and_topics(self) -> None:
        text = onboarding.start_message()

        self.assertIn("Vels Claude", text)
        self.assertIn("Claude Code", text)
        self.assertIn("топик", text.lower())
        # /projects убрана: light работает с одним проектом, он
        # привязывается автоматически.
        self.assertNotIn("/projects", text)
        self.assertIn("/status", text)
        self.assertIn("/settings", text)

    def test_auth_message_matches_vps_service_user_model(self) -> None:
        text = onboarding.auth_message(service_name="vels-claude")

        self.assertIn("production installer", text)
        self.assertIn("claude", text)
        self.assertIn("journalctl -u vels-claude", text)
        self.assertIn("systemctl status vels-claude", text)

    def test_no_projects_message_mentions_projects_dir_not_config_yaml_first(self) -> None:
        text = onboarding.no_projects_message(Path("/home/alice/projects"))
        # Normalise Windows backslashes so the path-shape assertion holds
        # across platforms — onboarding rendering uses str(Path), which
        # yields '\home\alice\projects' on Windows.
        normalised = text.replace("\\", "/")

        self.assertIn("/home/alice/projects", normalised)
        self.assertIn("/projects", normalised)
        self.assertNotIn("config.yaml", text)

    def test_project_ready_message_has_path_and_next_actions(self) -> None:
        text = onboarding.project_ready_message("demo", "/home/alice/projects/demo")

        self.assertIn("demo", text)
        self.assertIn("/home/alice/projects/demo", text)
        self.assertIn("/status", text)
        self.assertIn("/close", text)
        self.assertIn("файл", text.lower())

    def test_auto_topic_failure_is_actionable(self) -> None:
        text = onboarding.auto_topic_failure_message()

        self.assertIn("Topics", text)
        self.assertIn("BotFather", text)
        self.assertIn("топик", text.lower())

    def test_claude_cli_missing_error_mentions_installer_and_service_logs(self) -> None:
        text = onboarding.claude_error_message("cli_missing", "not found")

        self.assertIn("Claude Code CLI", text)
        self.assertIn("/auth", text)
        self.assertIn("journalctl", text)

    def test_rate_limit_keyword_fallback(self) -> None:
        text = onboarding.claude_error_message("unknown", "Rate limit exceeded")

        self.assertIn("лимит запросов", text.lower())

    def test_context_overflow_keyword_fallback(self) -> None:
        text = onboarding.claude_error_message("unknown", "context window exceeded")

        self.assertIn("Контекст сессии переполнен", text)

    def test_timeout_keyword_fallback(self) -> None:
        text = onboarding.claude_error_message("unknown", "request timed out")

        self.assertIn("не завершил ответ вовремя", text)


if __name__ == "__main__":
    unittest.main()
