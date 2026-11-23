"""MCP Streamable-HTTP client (JSON-RPC over POST; JSON or SSE replies)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote

import aiohttp

from .base import McpError, ToolError

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 30.0


def parse_sse(body: str) -> list[Any]:
    """Return the JSON payloads of every SSE event in ``body`` (multi-line data supported)."""
    out: list[Any] = []
    data: list[str] = []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            part = line[5:]
            data.append(part[1:] if part.startswith(" ") else part)
        elif line == "" and data:
            try:
                out.append(json.loads("\n".join(data)))
            except json.JSONDecodeError:
                pass
            data = []
    return out


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class McpClient:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        url: str,
        token_getter: Callable[[], Awaitable[str]],
        toolsets: Sequence[str],
        log: logging.Logger,
        client_name: str = "maubot-bitsai",
        client_version: str = "0.1.0",
    ) -> None:
        self._http = http
        self._url = f"{url}?toolsets={quote(','.join(toolsets), safe=',')}" if toolsets else url
        self._token_getter = token_getter
        self._log = log
        self._client_name = client_name
        self._client_version = client_version
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._initialized = False
        self._generation = 0
        self._init_lock = asyncio.Lock()

    # -- transport -------------------------------------------------------------------------

    async def _post(self, payload: dict[str, Any], timeout: float) -> tuple[int, str, str]:
        token = await self._token_getter()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        self._log.debug("MCP -> %s", payload.get("method"))
        try:
            async with self._http.post(
                self._url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                body = await r.text()
                ctype = r.headers.get("Content-Type", "")
                sid = r.headers.get("Mcp-Session-Id")
                if sid and payload.get("method") == "initialize":
                    self._session_id = sid
                status = r.status
                retry_after = r.headers.get("Retry-After")
        except TimeoutError as e:
            raise McpError(f"MCP request timed out after {timeout}s") from e
        except aiohttp.ClientError as e:
            raise McpError(f"MCP transport error: {type(e).__name__}") from e
        self._log.debug("MCP <- HTTP %s", status)
        if status >= 400:
            msg = f"MCP HTTP {status}"
            if status in (401, 403):
                msg += " (unauthorized)"
            raise McpError(msg, status=status, retry_after=_retry_after(retry_after))
        return status, ctype, body

    async def _rpc(self, method: str, params: dict[str, Any] | None, timeout: float,
                   *, init: bool = False, _retry: bool = True) -> Any:
        if not init:
            await self._ensure_initialized(timeout)
        gen = self._generation
        rid = next(self._ids)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            _, ctype, body = await self._post(payload, timeout)
        except McpError as e:
            if e.status == 404 and not init and _retry and self._session_id:
                await self._reset_session(gen)
                return await self._rpc(method, params, timeout, _retry=False)
            raise
        if "text/event-stream" in ctype:
            msgs = parse_sse(body)
        else:
            try:
                parsed = json.loads(body) if body.strip() else []
            except json.JSONDecodeError as e:
                raise McpError("MCP returned invalid JSON") from e
            msgs = parsed if isinstance(parsed, list) else [parsed]
        for m in msgs:
            if isinstance(m, dict) and m.get("id") == rid and "method" not in m:
                if "error" in m:
                    err = m["error"] or {}
                    raise McpError(str(err.get("message", "JSON-RPC error")),
                                   code=err.get("code"))
                return m.get("result")
        raise McpError(f"no JSON-RPC response for id {rid}")

    async def _reset_session(self, seen_generation: int) -> None:
        async with self._init_lock:
            # Another task may already have re-initialised since our request started.
            if self._generation == seen_generation:
                self._generation += 1
                self._initialized = False
                self._session_id = None
                self._protocol_version = None

    async def _ensure_initialized(self, timeout: float) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            res = await self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._client_name, "version": self._client_version},
            }, timeout, init=True)
            self._protocol_version = (res or {}).get("protocolVersion") or PROTOCOL_VERSION
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout)
            self._initialized = True

    # -- public API ------------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            res = await self._rpc("tools/list", {"cursor": cursor} if cursor else {},
                                  DEFAULT_TIMEOUT)
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, arguments: dict[str, Any],
                        timeout: float = 60.0) -> Any:
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}},
                              timeout) or {}
        content = res.get("content") or []
        text = ""
        if content and isinstance(content[0], dict):
            text = content[0].get("text") or ""
        if res.get("isError"):
            raise ToolError(text or "tool error")
        if not content:
            return res.get("structuredContent")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    async def close(self) -> None:
        """Drop session state. The shared aiohttp session is owned by the caller."""
        self._initialized = False
        self._session_id = None
        self._protocol_version = None
