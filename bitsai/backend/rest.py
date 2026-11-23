"""REST adapter for Datadog's public API (Bearer auth). Used for what MCP can't do well."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import aiohttp

from ..sites import Site
from .base import (
    AuthRequired,
    ChatTurn,
    Conclusion,
    Investigation,
    InvestigationStatus,
    MonitorAlertRef,
    PermissionDenied,
    RateLimited,
    ResourceKind,
    ResourceSummary,
    UpstreamError,
)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


def parse_investigation_status(raw: Any) -> InvestigationStatus:
    return InvestigationStatus.parse(raw)


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def monitor_summary(site: Site, data: dict) -> ResourceSummary:
    mid = _s(data.get("id"))
    fields = [("Type", _s(data.get("type")))]
    if data.get("priority") is not None:
        fields.append(("Priority", _s(data["priority"])))
    if data.get("query"):
        fields.append(("Query", _s(data["query"])))
    if data.get("tags"):
        fields.append(("Tags", ", ".join(map(str, data["tags"]))))
    return ResourceSummary(
        ResourceKind.MONITOR, mid, _s(data.get("name")) or f"Monitor {mid}",
        site.absolute(f"/monitors/{mid}"), _s(data.get("overall_state")) or None,
        tuple((k, v) for k, v in fields if v),
    )


def dashboard_summary(site: Site, data: dict, dash_id: str) -> ResourceSummary:
    did = _s(data.get("id") or data.get("dashboard_id") or dash_id)
    url = _s(data.get("url")) or f"/dashboard/{did}"
    fields = [("Author", _s(data.get("author_name"))),
              ("Layout", _s(data.get("layout_type") or data.get("reflow_type")))]
    return ResourceSummary(
        ResourceKind.DASHBOARD, did, _s(data.get("title")) or f"Dashboard {did}",
        site.absolute(url), None, tuple((k, v) for k, v in fields if v),
    )


def notebook_summary(site: Site, data: dict, nb_id: str) -> ResourceSummary:
    d = data.get("data", data)
    attrs = d.get("attributes", d) if isinstance(d, dict) else {}
    nid = _s(d.get("id") if isinstance(d, dict) and d.get("id") is not None else nb_id)
    author = (attrs.get("author") or {}).get("handle") if isinstance(
        attrs.get("author"), dict) else None
    fields = [("Status", _s(attrs.get("status"))), ("Author", _s(author))]
    return ResourceSummary(
        ResourceKind.NOTEBOOK, nid,
        _s(attrs.get("name") or attrs.get("title")) or f"Notebook {nid}",
        site.absolute(f"/notebook/{nid}"), None, tuple((k, v) for k, v in fields if v),
    )


def incident_summary(site: Site, data: dict, inc_id: str) -> ResourceSummary:
    d = data.get("data", data)
    attrs = d.get("attributes", d) if isinstance(d, dict) else {}
    iid = _s(d.get("id") if isinstance(d, dict) and d.get("id") else inc_id)
    pub = attrs.get("public_id")
    fields = [("Severity", _s((attrs.get("fields") or {}).get("severity", {}).get("value")
                              if isinstance(attrs.get("fields"), dict) else "")),
              ("Created", _s(attrs.get("created")))]
    return ResourceSummary(
        ResourceKind.INCIDENT, iid, _s(attrs.get("title")) or f"Incident {pub or iid}",
        site.absolute(f"/incidents/{pub or iid}"), _s(attrs.get("state")) or None,
        tuple((k, v) for k, v in fields if v),
    )


class RestBackend:
    """Partial :class:`BitsBackend`: investigations (start/get), lookups and snapshots."""

    def __init__(self, http: aiohttp.ClientSession, site: Site,
                 token_getter: Callable[[], Any]) -> None:
        self._http = http
        self._site = site
        self._token_getter = token_getter

    # ------------------------------------------------------------------ plumbing

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None) -> Any:
        token = self._token_getter()
        if inspect.isawaitable(token):
            token = await token
        if not token:
            raise AuthRequired("No Datadog credential available.")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with self._http.request(
                method, self._site.api_url + path, params=params, json=body,
                headers=headers, timeout=_TIMEOUT,
            ) as resp:
                status = resp.status
                if status == 401:
                    raise AuthRequired("Datadog rejected the credential (401).")
                if status == 403:
                    raise PermissionDenied("Datadog denied access (403): missing permission.")
                if status == 429:
                    ra = _retry_after(resp.headers.get("Retry-After")
                                      or resp.headers.get("X-RateLimit-Reset"))
                    raise RateLimited("Datadog rate limit hit.", ra)
                if status >= 400:
                    raise UpstreamError(f"Datadog returned HTTP {status}.")
                try:
                    return await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    raise UpstreamError("Datadog returned an unreadable response.") from None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise UpstreamError(f"Could not reach Datadog ({type(e).__name__}).") from None

    @staticmethod
    def _trigger_id(payload: Any) -> str:
        try:
            return str(payload["data"]["attributes"]["investigation_id"])
        except (KeyError, TypeError):
            raise UpstreamError("Unexpected response when starting investigation.") from None

    async def _trigger(self, trigger: dict) -> str:
        body = {"data": {"type": "trigger_investigation_request",
                         "attributes": {"trigger": trigger}}}
        return self._trigger_id(
            await self._request("POST", "/api/v2/bits-ai/investigations", body=body))

    # ------------------------------------------------------------------ investigations

    async def investigation_start_general(
        self, description: str, tags: list[str] | None = None,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> str:
        gen: dict[str, Any] = {"description": description}
        if tags:
            gen["tags"] = list(tags)
        if start_ms is not None and end_ms is not None:
            gen["start_time"] = start_ms
            gen["end_time"] = end_ms
        return await self._trigger({"type": "general_investigation", "general_investigation": gen})

    async def investigation_start_from_alert(self, alert: MonitorAlertRef) -> str:
        return await self._trigger({
            "type": "monitor_alert_trigger",
            "monitor_alert_trigger": {
                "monitor_id": alert.monitor_id,
                "event_id": alert.event_id,
                "event_ts": alert.event_ts_ms,
            },
        })

    async def investigation_get(self, investigation_id: str) -> Investigation:
        payload = await self._request("GET", f"/api/v2/bits-ai/investigations/{investigation_id}")
        try:
            data = payload["data"]
            attrs = data["attributes"]
            raw = _s(attrs.get("status"))
            concl = tuple(
                Conclusion(_s(c.get("title")), _s(c.get("summary")), _s(c.get("description")))
                for c in attrs.get("conclusions") or []
            )
            url = (payload.get("links") or {}).get("self")
            return Investigation(
                id=_s(data.get("id") or investigation_id),
                status=parse_investigation_status(raw), raw_status=raw,
                title=attrs.get("title"), conclusions=concl,
                url=self._site.absolute(url) if url else None,
            )
        except (KeyError, TypeError, AttributeError):
            raise UpstreamError("Unexpected investigation response shape.") from None

    async def investigation_steer(self, investigation_id: str, message: str) -> None:
        raise UpstreamError("not supported over REST")

    async def investigation_resume(self, investigation_id: str, context: str) -> None:
        raise UpstreamError("not supported over REST")

    # ------------------------------------------------------------------ chat

    async def chat_send(self, message: str, conversation_id: str | None = None,
                        pending_action_id: str | None = None) -> ChatTurn:
        raise UpstreamError("not supported over REST")

    async def chat_poll(self, conversation_id: str, task_id: str) -> ChatTurn:
        raise UpstreamError("not supported over REST")

    # ------------------------------------------------------------------ lookups

    async def resource_get(self, kind: ResourceKind, resource_id: str) -> ResourceSummary:
        rid = str(resource_id)
        if not rid or "/" in rid or ".." in rid:
            raise UpstreamError("Invalid resource id.")
        site = self._site
        try:
            if kind is ResourceKind.MONITOR:
                return monitor_summary(site, await self._request("GET", f"/api/v1/monitor/{rid}"))
            if kind is ResourceKind.DASHBOARD:
                return dashboard_summary(
                    site, await self._request("GET", f"/api/v1/dashboard/{rid}"), rid)
            if kind is ResourceKind.NOTEBOOK:
                return notebook_summary(
                    site, await self._request("GET", f"/api/v1/notebooks/{rid}"), rid)
            return incident_summary(
                site, await self._request("GET", f"/api/v2/incidents/{rid}"), rid)
        except (KeyError, TypeError, AttributeError):
            raise UpstreamError("Unexpected resource response shape.") from None

    async def graph_snapshot(self, metric_query: str, start_s: int, end_s: int) -> str | None:
        payload = await self._request(
            "GET", "/api/v1/graph/snapshot",
            params={"metric_query": metric_query, "start": str(start_s), "end": str(end_s)})
        url = payload.get("snapshot_url") if isinstance(payload, dict) else None
        return str(url) if url else None
