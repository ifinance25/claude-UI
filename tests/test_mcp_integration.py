from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.bot.handlers import callbacks, commands, messages
from src.bot.keyboards import create_mcp_catalog_keyboard
from src.claude.mcp import McpManager, detect_mcp_candidate


class McpManagerTests(unittest.TestCase):
    def test_detect_mcp_candidate_extracts_npm_package_url(self) -> None:
        candidate = detect_mcp_candidate(
            "Посмотри сюда https://www.npmjs.com/package/@modelcontextprotocol/server-github"
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.install_spec, "@modelcontextprotocol/server-github")
        self.assertEqual(candidate.server_name, "server-github")

    def test_install_and_remove_update_mcp_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "mcp.json"
            manager = McpManager(config_path=config_path)

            install_result = manager.install("@modelcontextprotocol/server-github")
            remove_result = manager.remove("server-github")

            self.assertTrue(install_result.created)
            self.assertTrue(remove_result.removed)

            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["mcpServers"], {})

    def test_detect_mcp_candidate_maps_official_github_repo_link_to_package(self) -> None:
        candidate = detect_mcp_candidate(
            "MCP https://github.com/modelcontextprotocol/servers/tree/main/src/github"
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.install_spec, "@modelcontextprotocol/server-github")


class McpKeyboardTests(unittest.TestCase):
    def test_catalog_keyboard_contains_catalog_actions(self) -> None:
        keyboard = create_mcp_catalog_keyboard({"github"})

        buttons = [
            (button.text, button.callback_data)
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertIn(("Install Browser", "mcp:install:browser"), buttons)
        self.assertIn(("Remove GitHub", "mcp:remove:github"), buttons)


class McpCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_cmd_mcp_without_args_shows_catalog(self) -> None:
        commands.router.mcp_manager = SimpleNamespace(
            describe_catalog=lambda: "MCP catalog",
            installed_server_names=lambda: {"github"},
        )
        message = SimpleNamespace(
            text="/mcp",
            answer=AsyncMock(),
        )

        await commands.cmd_mcp(message)

        message.answer.assert_awaited_once()
        self.assertIn("MCP catalog", message.answer.await_args.args[0])
        markup = message.answer.await_args.kwargs["reply_markup"]
        callback_data = {
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        }
        self.assertIn("mcp:install:browser", callback_data)


class McpCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_install_callback_installs_server_and_updates_message(self) -> None:
        manager = SimpleNamespace(
            install_catalog_tool=Mock(
                return_value=SimpleNamespace(message="Installed GitHub", created=True)
            ),
            describe_catalog=lambda: "MCP catalog",
            installed_server_names=lambda: {"github"},
        )
        callbacks.router.mcp_manager = manager

        callback = SimpleNamespace(
            data="mcp:install:github",
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )

        await callbacks.on_mcp_action(callback)

        manager.install_catalog_tool.assert_called_once_with("github")
        callback.answer.assert_awaited_once_with("Installed GitHub")
        callback.message.edit_text.assert_awaited_once()


class McpMessageHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_message_offers_install_for_detected_mcp_link(self) -> None:
        bridge = SimpleNamespace(send_message=Mock())
        messages.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False),
            get_light_project_paths=lambda: [],
        )
        messages.router.session_manager = SimpleNamespace(
            get_session=lambda topic_id: SimpleNamespace(
                project_name="demo",
                project_path="/tmp/demo",
                session_id="session-1",
                is_renamed=True,
                verbose_level=1,
            )
        )
        messages.router.claude_bridge = bridge
        messages.router.streamer = SimpleNamespace()
        messages.router.bot = SimpleNamespace(send_chat_action=AsyncMock())
        messages.router.mcp_manager = McpManager(config_path=Path("/tmp/unused-mcp-test.json"))

        message = SimpleNamespace(
            text="Добавь MCP https://github.com/modelcontextprotocol/servers/tree/main/src/github",
            chat=SimpleNamespace(id=555),
            message_thread_id=91,
            from_user=SimpleNamespace(username="tester", id=1),
            answer=AsyncMock(),
        )

        await messages.handle_message(message)

        bridge.send_message.assert_not_called()
        message.answer.assert_awaited_once()
        self.assertIn("Добавить MCP", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
