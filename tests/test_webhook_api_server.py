from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.config.settings import Settings
from src.webhooks.server import WebhookAPIServer, WebhookEvent


def _github_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookAPIServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[WebhookEvent] = []

    def _make_request(self, *, headers: dict[str, str], body: bytes) -> SimpleNamespace:
        async def read() -> bytes:
            return body

        async def json_loader() -> dict:
            return json.loads(body.decode("utf-8"))

        return SimpleNamespace(headers=headers, read=read, json=json_loader)

    async def _create_server(self, *, secret: str = "hook-secret") -> WebhookAPIServer:
        settings = Settings(
            telegram={"token": "bot-token"},
            security={"allowed_user_ids": [1]},
            webhooks={
                "host": "127.0.0.1",
                "port": 0,
                "secret": secret,
            },
        )
        return WebhookAPIServer(settings.webhooks, on_event=self.events.append)

    async def test_github_pull_request_webhook_validates_signature_and_parses_payload(self) -> None:
        server = await self._create_server()
        body = {
            "action": "opened",
            "number": 42,
            "pull_request": {
                "title": "Add webhook support",
                "html_url": "https://github.com/acme/project/pull/42",
                "user": {"login": "octocat"},
            },
            "repository": {"full_name": "acme/project"},
        }
        payload = json.dumps(body).encode("utf-8")

        response = await server._handle_github(
            self._make_request(
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-123",
                    "X-Hub-Signature-256": _github_signature("hook-secret", payload),
                    "Content-Type": "application/json",
                },
                body=payload,
            )
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.source, "github")
        self.assertEqual(event.kind, "pull_request.opened")
        self.assertEqual(event.repository, "acme/project")
        self.assertEqual(event.pull_request_number, 42)
        self.assertEqual(event.pull_request_author, "octocat")
        self.assertEqual(event.pull_request_title, "Add webhook support")
        self.assertEqual(event.delivery_id, "delivery-123")

    async def test_github_webhook_rejects_invalid_signature(self) -> None:
        server = await self._create_server()
        payload = json.dumps(
            {
                "action": "opened",
                "pull_request": {"user": {"login": "octocat"}},
                "repository": {"full_name": "acme/project"},
            }
        ).encode("utf-8")

        response = await server._handle_github(
            self._make_request(
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": "sha256=deadbeef",
                    "Content-Type": "application/json",
                },
                body=payload,
            )
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(self.events, [])

    async def test_custom_webhook_requires_shared_secret(self) -> None:
        server = await self._create_server()
        payload = json.dumps({"type": "ci.completed", "branch": "main"}).encode("utf-8")

        response = await server._handle_custom(
            self._make_request(
                headers={
                    "X-Webhook-Token": "wrong",
                    "Content-Type": "application/json",
                },
                body=payload,
            )
        )

        self.assertEqual(response.status, 401)
        self.assertEqual(self.events, [])

    async def test_custom_webhook_fail_closed_on_empty_secret(self) -> None:
        # Критично: при пустом secret /webhooks/custom НЕ принимает события
        # (раньше fail-open → неаутентифицированный приём → потенциальный RCE).
        server = await self._create_server(secret="")
        payload = json.dumps({"type": "ci.completed"}).encode("utf-8")

        response = await server._handle_custom(
            self._make_request(
                headers={"Content-Type": "application/json"},
                body=payload,
            )
        )

        self.assertEqual(response.status, 503)
        self.assertEqual(self.events, [])

    async def test_custom_webhook_accepts_with_correct_secret(self) -> None:
        server = await self._create_server(secret="hook-secret")
        payload = json.dumps({"type": "ci.completed", "branch": "main"}).encode("utf-8")

        response = await server._handle_custom(
            self._make_request(
                headers={
                    "X-Webhook-Token": "hook-secret",
                    "Content-Type": "application/json",
                },
                body=payload,
            )
        )

        self.assertEqual(response.status, 202)
        self.assertEqual(len(self.events), 1)

    async def test_webhook_defaults_are_failsafe(self) -> None:
        # Дефолты должны быть безопасными: выключен и слушает только loopback.
        from src.config.settings import WebhookSettings

        defaults = WebhookSettings()
        self.assertFalse(defaults.enabled)
        self.assertEqual(defaults.host, "127.0.0.1")

    async def test_settings_load_webhook_configuration_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                """
telegram:
  token: "bot-token"
security:
  allowed_user_ids:
    - 1
webhooks:
  enabled: true
  host: "0.0.0.0"
  port: 9000
  secret: "hook-secret"
""".strip()
            )

            settings = Settings.from_yaml(config_path)

        self.assertTrue(settings.webhooks.enabled)
        self.assertEqual(settings.webhooks.host, "0.0.0.0")
        self.assertEqual(settings.webhooks.port, 9000)
        self.assertEqual(settings.webhooks.secret, "hook-secret")


if __name__ == "__main__":
    unittest.main()
