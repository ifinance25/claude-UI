"""Exception hierarchy for Claude Code bridge errors."""
from __future__ import annotations


class ClaudeBridgeError(Exception):
    """Base exception for all Claude bridge errors."""

    def __init__(self, message: str = "", **kwargs: object) -> None:
        super().__init__(message)
        self.details = kwargs


class ClaudeRateLimitError(ClaudeBridgeError):
    """Raised when Claude API returns a rate limit error."""

    def __init__(
        self, message: str = "Rate limit exceeded", retry_after: float | None = None
    ) -> None:
        super().__init__(message, retry_after=retry_after)
        self.retry_after = retry_after


class ClaudeContextOverflowError(ClaudeBridgeError):
    """Raised when the conversation context exceeds the model limit."""

    def __init__(self, message: str = "Context window overflow") -> None:
        super().__init__(message)


class ClaudeAuthError(ClaudeBridgeError):
    """Raised on authentication or authorization failures."""

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message)


class ClaudeTimeoutError(ClaudeBridgeError):
    """Raised when a Claude operation times out."""

    def __init__(self, message: str = "Operation timed out") -> None:
        super().__init__(message)


class ClaudeStallError(ClaudeBridgeError):
    """Raised when the Claude process stalls (no output, waiting for input, etc.)."""

    def __init__(
        self,
        message: str = "Process stalled",
        kind: str = "unknown",
    ) -> None:
        super().__init__(message, kind=kind)
        self.kind = kind  # "waiting_input", "process_error", "no_output"
