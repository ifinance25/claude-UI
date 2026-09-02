"""`vels` CLI: вычисление адреса веб-платформы (issue #1)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.cli import _effective_url, _web_base, build_parser


def _settings(*, enabled=True, public="", host="127.0.0.1", port=8765):
    return SimpleNamespace(
        web=SimpleNamespace(
            enabled=enabled, public_origin=public, host=host, port=port
        )
    )


class CliUrlTests(unittest.TestCase):
    def test_public_origin_wins(self):
        s = _settings(public="https://agent.nickvels.ru/")
        public, local, enabled = _web_base(s)
        self.assertEqual(public, "https://agent.nickvels.ru")  # trailing slash trimmed
        self.assertEqual(local, "http://127.0.0.1:8765")
        self.assertTrue(enabled)
        self.assertEqual(_effective_url(s), "https://agent.nickvels.ru")

    def test_falls_back_to_local_bind(self):
        s = _settings(public="", host="0.0.0.0", port=9000)
        public, local, _ = _web_base(s)
        self.assertIsNone(public)
        self.assertEqual(local, "http://0.0.0.0:9000")
        self.assertEqual(_effective_url(s), "http://0.0.0.0:9000")

    def test_parser_subcommands(self):
        parser = build_parser()
        for cmd in ("url", "open", "status"):
            ns = parser.parse_args([cmd])
            self.assertEqual(ns.command, cmd)


if __name__ == "__main__":
    unittest.main()
