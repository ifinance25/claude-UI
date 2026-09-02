"""Embedded webhook API server for external events."""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from typing import Any, Awaitable, Callable, Literal

import structlog
from aiohttp import web
from pydantic import BaseModel, Field

from src.config.settings import WebhookSettings

logger = structlog.get_logger()


class WebhookEvent(BaseModel):
    """Normalized webhook event."""

    source: Literal["github", "custom"]
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    delivery_id: str | None = None
    repository: str | None = None
    action: str | None = None
    pull_request_number: int | None = None
    pull_request_title: str | None = None
    pull_request_author: str | None = None
    pull_request_url: str | None = None
    ref: str | None = None


WebhookHandler = Callable[[WebhookEvent], Awaitable[None] | None]


def verify_github_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Verify a GitHub `X-Hub-Signature-256` header."""
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    actual = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, actual)


def parse_github_event(
    payload: dict[str, Any],
    *,
    event_name: str,
    delivery_id: str | None = None,
) -> WebhookEvent:
    """Normalize a GitHub webhook payload."""
    event_name = event_name.strip().lower()

    if event_name == "pull_request":
        pull_request = payload.get("pull_request") or {}
        repository = payload.get("repository") or {}
        return WebhookEvent(
            source="github",
            kind=f"pull_request.{payload.get('action') or 'unknown'}",
            payload=payload,
            delivery_id=delivery_id,
            repository=repository.get("full_name"),
            action=payload.get("action"),
            pull_request_number=payload.get("number"),
            pull_request_title=pull_request.get("title"),
            pull_request_author=(pull_request.get("user") or {}).get("login"),
            pull_request_url=pull_request.get("html_url"),
        )

    if event_name == "push":
        repository = payload.get("repository") or {}
        return WebhookEvent(
            source="github",
            kind="push",
            payload=payload,
            delivery_id=delivery_id,
            repository=repository.get("full_name"),
            action="push",
            ref=payload.get("ref"),
        )

    repository = payload.get("repository") or {}
    return WebhookEvent(
        source="github",
        kind=event_name or "github.unknown",
        payload=payload,
        delivery_id=delivery_id,
        repository=repository.get("full_name"),
        action=payload.get("action"),
        ref=payload.get("ref"),
    )


def parse_custom_event(payload: dict[str, Any], *, delivery_id: str | None = None) -> WebhookEvent:
    """Normalize a custom webhook payload."""
    kind = str(payload.get("kind") or payload.get("type") or "custom.event")
    repository_value = payload.get("repository")
    repository = repository_value.get("full_name") if isinstance(repository_value, dict) else repository_value
    return WebhookEvent(
        source="custom",
        kind=kind,
        payload=payload,
        delivery_id=delivery_id,
        repository=repository,
        action=payload.get("action"),
        ref=payload.get("ref"),
    )


class WebhookAPIServer:
    """Small embedded webhook server built on aiohttp."""

    def __init__(
        self,
        settings: WebhookSettings,
        on_event: WebhookHandler | None = None,
    ) -> None:
        self.settings = settings
        self._on_event = on_event
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._app = web.Application()
        self._app.add_routes(
            [
                web.get("/health", self._handle_health),
                web.post("/webhooks/github", self._handle_github),
                web.post("/webhooks/custom", self._handle_custom),
            ]
        )

    @property
    def app(self) -> web.Application:
        return self._app

    async def start(self) -> None:
        """Start listening in the background."""
        if not self.settings.enabled:
            logger.info("webhook_api_disabled")
            return
        if self._runner is not None:
            return

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.settings.host, self.settings.port)
        await self._site.start()

        logger.info(
            "webhook_api_started",
            host=self.settings.host,
            port=self.settings.port,
        )

    async def stop(self) -> None:
        """Stop the embedded server."""
        if self._runner is None:
            return

        await self._runner.cleanup()
        self._runner = None
        self._site = None
        logger.info("webhook_api_stopped")

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_github(self, request: web.Request) -> web.Response:
        if self.settings.secret == "":
            return web.json_response(
                {"error": "webhook secret not configured"},
                status=503,
            )

        body = await request.read()
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_github_signature(self.settings.secret, body, signature):
            return web.json_response({"error": "invalid signature"}, status=401)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        event_name = request.headers.get("X-GitHub-Event", "")
        delivery_id = request.headers.get("X-GitHub-Delivery")
        event = parse_github_event(
            payload,
            event_name=event_name,
            delivery_id=delivery_id,
        )

        await self._emit_event(event)
        return web.json_response({"status": "accepted", "kind": event.kind}, status=202)

    async def _handle_custom(self, request: web.Request) -> web.Response:
        # Fail-closed: при пустом secret НЕ принимаем события (раньше блок
        # проверки просто пропускался → неаутентифицированный приём, который
        # ведёт к запуску Claude с bypassPermissions). Симметрично _handle_github.
        if self.settings.secret == "":
            return web.json_response(
                {"error": "webhook secret not configured"},
                status=503,
            )
        token = request.headers.get("X-Webhook-Token") or request.headers.get(
            "Authorization", ""
        )
        if token.startswith("Bearer "):
            token = token.removeprefix("Bearer ").strip()
        # Сравнение в константное время — чтобы нельзя было подобрать секрет по
        # таймингу.
        if not hmac.compare_digest(token, self.settings.secret):
            return web.json_response({"error": "invalid token"}, status=401)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        event = parse_custom_event(
            payload,
            delivery_id=request.headers.get("X-Webhook-Delivery"),
        )
        await self._emit_event(event)
        return web.json_response({"status": "accepted", "kind": event.kind}, status=202)

    async def _emit_event(self, event: WebhookEvent) -> None:
        if self._on_event is None:
            logger.info(
                "webhook_event_received",
                source=event.source,
                kind=event.kind,
                repository=event.repository,
            )
            return

        result = self._on_event(event)
        if inspect.isawaitable(result):
            await result
