"""MCP adapter: chat + investigations over Datadog's remote MCP server."""

from __future__ import annotations

import re
from typing import Any

from ..mcp.base import McpError, McpSession, ToolError
from ..sites import Site
from .base import (
    AuthRequired,
    BackendError,
    ChatStatus,
    ChatTurn,
    Conclusion,
    Investigation,
    MonitorAlertRef,
    PendingAction,
    PermissionDenied,
    RateLimited,
    ResourceKind,
    ResourceSummary,
    UpstreamError,
)
from .rest import (
    RestBackend,
    dashboard_summary,
    incident_summary,
    monitor_summary,
    notebook_summary,
    parse_investigation_status,
)

_TOOL_TAG = re.compile(r"<tool\s+id=\"[^\"]*\"\s*/>")


def _text(value: Any) -> str:
    """MCP text may be a string or a list of ``{text, type}`` blocks."""
    if value is None:
        return ""
    if isinstance(value, str):
        out = value
    elif isinstance(value, list):
        out = "\n\n".join(_text(v) for v in value)
    elif isinstance(value, dict):
        out = str(value.get("text", ""))
    else:
        out = str(value)
    return _TOOL_TAG.sub("", out).strip()


def _map_error(err: Exception) -> BackendError:
    if isinstance(err, BackendError):
        return err
    if isinstance(err, ToolError):
        # Tool error text is produced by Datadog for the user; keep it short.
        msg = " ".join(str(err).split())[:200]
        return UpstreamError(f"Datadog tool error: {msg}" if msg else "Datadog tool error.")
    if isinstance(err, McpError):
        if err.status == 401:
            return AuthRequired("Datadog rejected the credential (401).")
        if err.status == 403:
            return PermissionDenied("Datadog denied access (403): missing permission.")
        if err.status == 429:
            return RateLimited("Datadog rate limit hit.", err.retry_after)
        return UpstreamError(
            f"Datadog MCP error (HTTP {err.status})." if err.status else "Datadog MCP error.")
    return UpstreamError(f"Unexpected Datadog failure ({type(err).__name__}).")


class McpBackend:
    def __init__(self, session: McpSession, site: Site, rest: RestBackend | None = None) -> None:
        self._session = session
        self._site = site
        self._rest = rest

    async def _call(self, tool: str, intent: str, args: dict[str, Any],
                    timeout: float = 60.0) -> Any:
        full = dict(args)
        full["telemetry"] = {"intent": intent}
        try:
            return await self._session.call_tool(tool, full, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - mapped to safe BackendError
            raise _map_error(e) from None

    # ------------------------------------------------------------------ chat

    def _turn(self, d: Any) -> ChatTurn:
        if not isinstance(d, dict):
            raise UpstreamError("Unexpected chat response.")
        try:
            status = ChatStatus(str(d.get("status", "")).lower())
        except ValueError:
            status = ChatStatus.UNKNOWN
        pa = d.get("pending_action")
        pending = None
        if isinstance(pa, dict) and pa.get("id") is not None:
            pending = PendingAction(str(pa["id"]), _text(pa.get("description")), raw=pa)
        na = d.get("next_action")
        poll = na.get("poll_after_seconds") if isinstance(na, dict) else None
        err = d.get("error_message") or d.get("error_code")
        return ChatTurn(
            conversation_id=str(d.get("conversation_id") or ""),
            task_id=str(d.get("task_id") or ""),
            status=status,
            message=d.get("message"),
            pending_action=pending,
            error=str(err) if err else None,
            poll_after=float(poll) if isinstance(poll, (int, float)) else None,
        )

    async def chat_send(self, message: str, conversation_id: str | None = None,
                        pending_action_id: str | None = None) -> ChatTurn:
        args: dict[str, Any] = {"message": message}
        if conversation_id:
            args["conversation_id"] = conversation_id
        if pending_action_id:
            args["pending_action_id"] = pending_action_id
        return self._turn(await self._call(
            "send_message_to_chat", "Send a user's message to Bits AI chat", args))

    async def chat_poll(self, conversation_id: str, task_id: str) -> ChatTurn:
        return self._turn(await self._call(
            "get_chat_message_result", "Check for the result of a Bits AI chat message",
            {"conversation_id": conversation_id, "task_id": task_id}, timeout=45.0))

    # ------------------------------------------------------------------ investigations

    @staticmethod
    def _inv_id(d: Any) -> str:
        if isinstance(d, dict):
            for src in (d, d.get("data") if isinstance(d.get("data"), dict) else {}):
                attrs = src.get("attributes") if isinstance(src.get("attributes"), dict) else src
                for key in ("investigation_id", "uuid"):
                    if attrs.get(key):
                        return str(attrs[key])
        raise UpstreamError("Unexpected response when starting investigation.")

    async def investigation_start_general(
        self, description: str, tags: list[str] | None = None,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> str:
        args: dict[str, Any] = {"description": description}
        if tags:
            args["tags"] = list(tags)
        if start_ms is not None and end_ms is not None:
            args["start_time"] = start_ms
            args["end_time"] = end_ms
        return self._inv_id(await self._call(
            "trigger_general_investigation",
            "Start a Bits AI investigation requested by a chat user", args))

    async def investigation_start_from_alert(self, alert: MonitorAlertRef) -> str:
        return self._inv_id(await self._call(
            "trigger_bits_ai_investigation",
            "Start a Bits AI investigation for a monitor alert",
            {"monitor_id": alert.monitor_id, "event_id": alert.event_id,
             "event_ts": alert.event_ts_ms}))

    async def investigation_get(self, investigation_id: str) -> Investigation:
        # The MCP payload (~60 KB, text blocks, no title/summary split) is ambiguous;
        # REST has a stable shape and the deep link, so prefer it when available.
        if self._rest is not None:
            try:
                return await self._rest.investigation_get(investigation_id)
            except (AuthRequired, PermissionDenied, RateLimited):
                raise
            except UpstreamError:
                pass
        d = await self._call(
            "get_bits_ai_investigation", "Check the status of a Bits AI investigation",
            {"investigation_id": investigation_id})
        try:
            data = d["data"]
            inv = data["attributes"]["response"]["investigation"]
            raw = str(inv.get("status") or "")
            concl = []
            for c in inv.get("conclusions") or []:
                # MCP: summary = headline, message = short summary, description = detail.
                concl.append(Conclusion(_text(c.get("summary")), _text(c.get("message")),
                                        _text(c.get("description"))))
            iid = str(data.get("id") or inv.get("uuid") or investigation_id)
            return Investigation(
                id=iid, status=parse_investigation_status(raw), raw_status=raw,
                title=_text(inv.get("title")) or None, conclusions=tuple(concl),
                url=self._site.absolute(f"/bits-ai/investigations/{iid}"),
            )
        except (KeyError, TypeError, AttributeError):
            raise UpstreamError("Unexpected investigation response shape.") from None

    async def investigation_steer(self, investigation_id: str, message: str) -> None:
        await self._call(
            "steer_bits_ai_investigation", "Send guidance to a running Bits AI investigation",
            {"investigation_id": investigation_id, "message": message})

    async def investigation_resume(self, investigation_id: str, context: str) -> None:
        await self._call(
            "resume_bits_investigation", "Resume a finished Bits AI investigation with new context",
            {"investigation_id": investigation_id, "additional_context": context})

    # ------------------------------------------------------------------ lookups

    async def resource_get(self, kind: ResourceKind, resource_id: str) -> ResourceSummary:
        rid = str(resource_id)
        if kind is ResourceKind.DASHBOARD:
            d = await self._call(
                "get_datadog_dashboard", "Look up a dashboard to show a link preview",
                {"dashboard_id": rid, "include_widgets": False})
            if not isinstance(d, dict):
                raise UpstreamError("Unexpected dashboard response.")
            return dashboard_summary(self._site, d, rid)
        if self._rest is not None:
            return await self._rest.resource_get(kind, rid)
        if kind is ResourceKind.NOTEBOOK:
            d = await self._call(
                "get_datadog_notebook", "Look up a notebook to show a link preview",
                {"notebook_id": rid, "max_tokens": 1000})
            if not isinstance(d, dict):
                raise UpstreamError("Unexpected notebook response.")
            return notebook_summary(self._site, d, rid)
        if kind is ResourceKind.INCIDENT:
            d = await self._call(
                "get_datadog_incident", "Look up an incident to show a link preview",
                {"incident_id": rid, "max_tokens": 1000})
            if not isinstance(d, dict):
                raise UpstreamError("Unexpected incident response.")
            return incident_summary(self._site, d, rid)
        d = await self._call(
            "search_datadog_monitors", "Look up a monitor to show a link preview",
            {"query": rid, "max_tokens": 1000})
        for m in d if isinstance(d, list) else []:
            if isinstance(m, dict) and str(m.get("id")) == rid:
                return monitor_summary(
                    self._site, {**m, "overall_state": m.get("status")})
        raise UpstreamError("Monitor not found.")

    async def graph_snapshot(self, metric_query: str, start_s: int, end_s: int) -> str | None:
        if self._rest is None:
            return None
        return await self._rest.graph_snapshot(metric_query, start_s, end_s)
