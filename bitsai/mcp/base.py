"""MCP client contract (frozen). Implemented in ``bitsai/mcp/client.py``."""

from __future__ import annotations

from typing import Any, Protocol


class McpError(Exception):
    """Transport or JSON-RPC error. ``status`` is the HTTP status if one was received."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class ToolError(McpError):
    """The tool ran but returned ``isError: true``. ``str()`` is the tool's error text."""


class McpSession(Protocol):
    """One authenticated MCP session (one user, one toolset selection).

    - Lazily runs ``initialize`` + ``notifications/initialized`` on first use and keeps the
      ``Mcp-Session-Id``; transparently re-initialises once if the server reports the session
      expired (HTTP 404 with a session id, per the MCP spec).
    - Accepts JSON or SSE (``text/event-stream``) responses for any request.
    - Never logs the bearer token or full payloads above DEBUG.
    """

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float = 60.0) -> Any:
        """Call a tool and return the **decoded** result: ``content[0].text`` parsed as JSON
        if it is JSON, else the raw text. Raises ToolError on ``isError``."""
        ...

    async def close(self) -> None: ...
