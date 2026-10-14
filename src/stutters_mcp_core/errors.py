"""Exceptions whose messages are meant for the model.

Subclassing ToolError makes FastMCP return them as tool errors (no traceback logged). Messages should
say what went wrong *and* what to do next.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class InvalidRequest(ToolError):
    """The tool arguments were invalid (unknown type, bad path, conflicting options, ...)."""


class WritesDisabled(ToolError):
    """A write was attempted on a server running read-only."""


class UpstreamError(ToolError):
    """An error talking to the managed product's API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthError(UpstreamError):
    """401/403, or a failed login."""


class NotFound(UpstreamError):
    """404."""


class Conflict(UpstreamError):
    """409: the object exists, is in use, or changed concurrently."""


class ValidationFailed(UpstreamError):
    """400/422: the product rejected the payload."""


class RateLimited(UpstreamError):
    """429 after all retries."""


class Unreachable(UpstreamError):
    """Connection refused, DNS failure, TLS failure."""


class UpstreamTimeout(UpstreamError):
    """The request (or a polled task) did not finish in time."""


class TaskFailed(UpstreamError):
    """An asynchronous job on the product finished unsuccessfully."""
