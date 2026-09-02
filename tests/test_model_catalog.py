"""Живой каталог моделей: имена из API, мягкая деградация, строгий allowlist.

Регрессия, которую закрывают эти тесты: модели выходят чаще, чем правится
статический ``KNOWN_MODELS``, и новая модель приезжала в пикер сырым id
(``claude-fable-5-1``) — либо не приезжала вовсе.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from src.claude import model_catalog
from src.claude.models import KNOWN_MODELS


class ModelCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        model_catalog.reset_cache()
        self.addCleanup(model_catalog.reset_cache)

    def test_falls_back_to_static_without_api_key(self) -> None:
        """Подписочный вход ключа не даёт — это норма, а не ошибка."""
        with mock.patch.object(model_catalog, "_resolve_api_key", return_value=None):
            got = model_catalog.get_models()
        self.assertEqual([m["id"] for m in got], [m["id"] for m in KNOWN_MODELS])

    def test_new_model_appears_with_display_name(self) -> None:
        """Новая модель подхватывается автоматически и с человеческим именем."""
        remote = [
            {"id": "claude-fable-5-1", "label": "Claude Fable 5.1"},
            {"id": "claude-opus-5", "label": "Claude Opus 5"},
        ]
        with mock.patch.object(model_catalog, "_load_remote", return_value=remote):
            got = model_catalog.get_models()
        by_id = {m["id"]: m for m in got}
        self.assertIn("claude-fable-5-1", by_id)
        self.assertEqual(by_id["claude-fable-5-1"]["label"], "Claude Fable 5.1")
        # Подсказку берём из статики по семейству — пикер не остаётся без описания.
        self.assertTrue(by_id["claude-fable-5-1"]["hint"])
        # Порядок API (новые первыми) сохраняется.
        self.assertEqual(got[0]["id"], "claude-fable-5-1")
        # Allowlist для записи в settings.json растёт вместе с каталогом.
        self.assertIn("claude-fable-5-1", model_catalog.catalog_model_ids())
        self.assertEqual(model_catalog.label_for("claude-fable-5-1"), "Claude Fable 5.1")

    def test_api_failure_keeps_picker_working(self) -> None:
        """Нет сети / 401 — пикер не пустеет и не падает."""
        with mock.patch.object(
            model_catalog, "_resolve_api_key", return_value="sk-ant-test"
        ), mock.patch.object(
            model_catalog, "fetch_remote_models", side_effect=OSError("no network")
        ):
            got = model_catalog.get_models()
        self.assertEqual([m["id"] for m in got], [m["id"] for m in KNOWN_MODELS])

    def test_default_model_always_selectable(self) -> None:
        """Модель по умолчанию есть в каталоге, даже если API её не вернул."""
        with mock.patch.object(
            model_catalog, "_load_remote", return_value=[{"id": "claude-x-9", "label": "X"}]
        ):
            ids = {m["id"] for m in model_catalog.get_models()}
        self.assertIn(model_catalog.DEFAULT_MODEL, ids)

    def test_fetch_skips_malformed_ids_and_paginates(self) -> None:
        """Ответ API — недоверенный вход: id уезжает в settings.json.

        Мусорные id отбрасываются, пустой display_name достраивается
        гуманизатором, пагинация проходится до конца.
        """
        pages = [
            {
                "data": [
                    {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1"},
                    {"id": "../../etc/passwd", "display_name": "evil"},
                    {"id": "claude opus 5", "display_name": "spaced"},
                ],
                "has_more": True,
                "last_id": "claude-fable-5-1",
            },
            {
                "data": [{"id": "claude-haiku-4-5-20251001", "display_name": ""}],
                "has_more": False,
            },
        ]
        calls: list[str] = []

        def fake_urlopen(req, timeout=None):  # noqa: ANN001, ARG001
            calls.append(req.full_url)
            body = json.dumps(pages[len(calls) - 1]).encode()
            resp = io.BytesIO(body)
            resp.__enter__ = lambda self=resp: self  # type: ignore[attr-defined]
            resp.__exit__ = lambda *a, **k: False  # type: ignore[attr-defined]
            return resp

        with mock.patch.object(model_catalog.urllib.request, "urlopen", fake_urlopen):
            got = model_catalog.fetch_remote_models(api_key="sk-ant-test")

        self.assertEqual(
            got,
            [
                {"id": "claude-fable-5-1", "label": "Claude Fable 5.1"},
                {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5"},
            ],
        )
        self.assertEqual(len(calls), 2)
        self.assertIn("after_id=claude-fable-5-1", calls[1])

    def test_fetch_sends_api_headers(self) -> None:
        captured: dict[str, str] = {}

        def fake_urlopen(req, timeout=None):  # noqa: ANN001, ARG001
            captured.update({k.lower(): v for k, v in req.header_items()})
            resp = io.BytesIO(json.dumps({"data": [], "has_more": False}).encode())
            resp.__enter__ = lambda self=resp: self  # type: ignore[attr-defined]
            resp.__exit__ = lambda *a, **k: False  # type: ignore[attr-defined]
            return resp

        with mock.patch.object(model_catalog.urllib.request, "urlopen", fake_urlopen):
            model_catalog.fetch_remote_models(api_key="sk-ant-test")
        self.assertEqual(captured.get("X-api-key".lower()), "sk-ant-test")
        self.assertEqual(captured.get("Anthropic-version".lower()), "2023-06-01")


if __name__ == "__main__":
    unittest.main()
