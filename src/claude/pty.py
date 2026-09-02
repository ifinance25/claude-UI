"""Helpers for normalizing PTY output into clean lines."""
from __future__ import annotations


class TerminalOutputBuffer:
    """Collect PTY chunks, stripping ANSI sequences and handling redraws."""

    def __init__(self) -> None:
        self._current_line = ""
        self._escape_state: str | None = None
        self._osc_saw_escape = False
        self._pending_cr = False

    def push(self, text: str) -> list[str]:
        lines: list[str] = []

        for char in text:
            if self._escape_state is not None:
                self._consume_escape(char)
                continue

            if char == "\x1b":
                self._escape_state = "escape"
                self._osc_saw_escape = False
                continue

            # Handle deferred \r: PTY sends \r\n as line terminator (ONLCR).
            # Only clear the line on standalone \r (real carriage return),
            # not when \r is followed by \n.
            if self._pending_cr:
                self._pending_cr = False
                if char == "\n":
                    # \r\n — standard line terminator, emit the line
                    lines.append(self._current_line)
                    self._current_line = ""
                    continue
                else:
                    # Standalone \r — real carriage return, clear line
                    self._current_line = ""
                    # Fall through to process current char normally

            if char == "\r":
                self._pending_cr = True
                continue

            if char == "\n":
                lines.append(self._current_line)
                self._current_line = ""
                continue

            self._current_line += char

        return lines

    def flush(self) -> str | None:
        if not self._current_line:
            return None
        line = self._current_line
        self._current_line = ""
        return line

    def snapshot(self) -> str:
        return self._current_line

    def _consume_escape(self, char: str) -> None:
        if self._escape_state == "escape":
            if char == "[":
                self._escape_state = "csi"
                return
            if char == "]":
                self._escape_state = "osc"
                return
            self._escape_state = None
            return

        if self._escape_state == "csi":
            if "@" <= char <= "~":
                self._escape_state = None
            return

        if self._escape_state == "osc":
            if self._osc_saw_escape:
                self._osc_saw_escape = False
                if char == "\\":
                    self._escape_state = None
                return
            if char == "\x07":
                self._escape_state = None
                return
            if char == "\x1b":
                self._osc_saw_escape = True
