from __future__ import annotations

import unittest

from src.claude.pty import TerminalOutputBuffer


class TerminalOutputBufferTests(unittest.TestCase):
    def test_strips_ansi_sequences_even_when_split_across_chunks(self) -> None:
        buffer = TerminalOutputBuffer()

        self.assertEqual(
            buffer.push('\x1b[31m{"type":"text_delta","text":"hel'),
            [],
        )
        self.assertEqual(
            buffer.push('lo"}\x1b[0m\n'),
            ['{"type":"text_delta","text":"hello"}'],
        )

    def test_carriage_return_overwrites_current_line(self) -> None:
        buffer = TerminalOutputBuffer()

        lines = buffer.push("progress 1%\rprogress 2%\n")

        self.assertEqual(lines, ["progress 2%"])
        self.assertEqual(buffer.snapshot(), "")


if __name__ == "__main__":
    unittest.main()
