import json
from typing import Any

import pytest

from bitsai.backend.base import (
    AuthRequired,
    ChatStatus,
    InvestigationStatus,
    MonitorAlertRef,
    PermissionDenied,
    RateLimited,
    ResourceKind,
    UpstreamError,
)
from bitsai.backend.mcp import McpBackend
from bitsai.mcp.base import McpError, ToolError
from bitsai.sites import Site

SITE = Site("datadoghq.com", "acme")


def decoded(fx):
    return json.loads(fx["result"]["content"][0]["text"] if "result" in fx
                      else fx["content"][0]["text"])


class FakeSession:
    def __init__(self, responses: dict[str, Any] | None = None, error: Exception | None = None):
        self.responses = responses or {}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments, timeout=60.0):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.responses[name]

    async def close(self):
        pass


async def test_chat_send_queued(load_fixture):
    s = FakeSession({"send_message_to_chat": decoded(load_fixture("chat_send_1.json"))})
    turn = await McpBackend(s, SITE, None).chat_send("hi", conversation_id="c1")
    assert turn.status is ChatStatus.QUEUED and not turn.status.terminal
    assert turn.poll_after == 2 and turn.task_id and turn.pending_action is None
    name, args = s.calls[0]
    assert args["message"] == "hi" and args["conversation_id"] == "c1"
    assert args["telemetry"]["intent"]


async def test_chat_poll_completed(load_fixture):
    s = FakeSession({"get_chat_message_result":
                     decoded(load_fixture("chat_result_completed_1.json"))})
    turn = await McpBackend(s, SITE).chat_poll("c", "t")
    assert turn.status is ChatStatus.COMPLETED and turn.message.startswith("None.")
    assert turn.error is None


async def test_chat_unknown_status_and_pending_action():
    s = FakeSession({"get_chat_message_result": {"status": "weird", "task_id": "t",
                                                 "conversation_id": "c"}})
    assert (await McpBackend(s, SITE).chat_poll("c", "t")).status is ChatStatus.UNKNOWN
    s = FakeSession({"send_message_to_chat": {
        "status": "requires_action", "task_id": "t", "conversation_id": "c",
        "pending_action": {"id": "pa1", "description": "Mute monitor", "target_tool": "x"}}})
    turn = await McpBackend(s, SITE).chat_send("approve", pending_action_id="pa1")
    assert turn.status is ChatStatus.REQUIRES_ACTION
    assert turn.pending_action.id == "pa1" and turn.pending_action.description == "Mute monitor"
    assert s.calls[0][1]["pending_action_id"] == "pa1"


async def test_chat_failed_error():
    s = FakeSession({"get_chat_message_result": {
        "status": "failed", "task_id": "t", "conversation_id": "c", "error_message": "boom"}})
    turn = await McpBackend(s, SITE).chat_poll("c", "t")
    assert turn.status.terminal and turn.error == "boom"


async def test_investigation_start_and_steer_resume_telemetry():
    s = FakeSession({
        "trigger_general_investigation": {"investigation_id": "inv1"},
        "trigger_bits_ai_investigation": {"investigation_id": "inv2"},
        "steer_bits_ai_investigation": {"ok": True},
        "resume_bits_investigation": {"ok": True},
    })
    b = McpBackend(s, SITE)
    assert await b.investigation_start_general("d", tags=["service:x"], start_ms=1, end_ms=2) \
        == "inv1"
    assert await b.investigation_start_from_alert(MonitorAlertRef(5, "1" * 16, 99)) == "inv2"
    await b.investigation_steer("inv1", "look at db")
    await b.investigation_resume("inv1", "more")
    assert s.calls[0][1]["start_time"] == 1 and s.calls[0][1]["tags"] == ["service:x"]
    assert s.calls[1][1]["monitor_id"] == 5 and s.calls[1][1]["event_ts"] == 99
    assert s.calls[2][1]["message"] == "look at db"
    assert s.calls[3][1]["additional_context"] == "more"
    assert all(a["telemetry"]["intent"] for _, a in s.calls)
    assert len({a["telemetry"]["intent"] for _, a in s.calls}) == 4


async def test_investigation_get_mcp_payload(load_fixture):
    s = FakeSession({"get_bits_ai_investigation":
                     decoded(load_fixture("investigation_mcp_get_conclusive.json"))})
    inv = await McpBackend(s, SITE, None).investigation_get("x")
    assert inv.status is InvestigationStatus.CONCLUSIVE and inv.raw_status == "conclusive"
    assert inv.url == f"https://acme.datadoghq.com/bits-ai/investigations/{inv.id}"
    assert len(inv.conclusions) == 1 and inv.conclusions[0].description
    assert s.calls[0][1]["telemetry"]["intent"]


async def test_investigation_get_prefers_rest():
    class Rest:
        async def investigation_get(self, i):
            return "from-rest"

    s = FakeSession()
    assert await McpBackend(s, SITE, Rest()).investigation_get("x") == "from-rest"
    assert s.calls == []


@pytest.mark.parametrize("raw,expected", [
    ("in progress", InvestigationStatus.IN_PROGRESS),
    ("IN_PROGRESS", InvestigationStatus.IN_PROGRESS),
    ("Needs Direction", InvestigationStatus.NEEDS_DIRECTION),
    ("inconclusive", InvestigationStatus.INCONCLUSIVE),
    ("failed", InvestigationStatus.FAILED),
    ("whatever", InvestigationStatus.UNKNOWN),
])
async def test_investigation_status_mapping(raw, expected):
    payload = {"data": {"id": "i", "attributes": {"response": {"investigation": {
        "status": raw, "conclusions": []}}}}}
    s = FakeSession({"get_bits_ai_investigation": payload})
    inv = await McpBackend(s, SITE).investigation_get("i")
    assert inv.status is expected and inv.raw_status == raw


async def test_resource_get_dashboard_and_rest_fallback(load_fixture):
    s = FakeSession({"get_datadog_dashboard":
                     decoded(load_fixture("getter_mcp_get_datadog_dashboard.json"))})
    r = await McpBackend(s, SITE).resource_get(ResourceKind.DASHBOARD, "abc-def-ghi")
    assert r.title == "JM/MM Dashboard" and r.url.startswith("https://")
    assert s.calls[0][1]["telemetry"]["intent"]

    class Rest:
        async def resource_get(self, kind, rid):
            return ("rest", kind, rid)

    assert await McpBackend(s, SITE, Rest()).resource_get(ResourceKind.MONITOR, "1") == (
        "rest", ResourceKind.MONITOR, "1")


async def test_resource_get_monitor_via_mcp_search(load_fixture):
    s = FakeSession({"search_datadog_monitors":
                     decoded(load_fixture("getter_mcp_search_datadog_monitors.json"))})
    r = await McpBackend(s, SITE).resource_get(ResourceKind.MONITOR, "100000001")
    assert r.status == "OK" and r.url == "https://acme.datadoghq.com/monitors/100000001"
    with pytest.raises(UpstreamError):
        await McpBackend(s, SITE).resource_get(ResourceKind.MONITOR, "42")


async def test_graph_snapshot():
    assert await McpBackend(FakeSession(), SITE).graph_snapshot("q", 1, 2) is None

    class Rest:
        async def graph_snapshot(self, q, a, b):
            return "https://png"

    assert await McpBackend(FakeSession(), SITE, Rest()).graph_snapshot("q", 1, 2) \
        == "https://png"


@pytest.mark.parametrize("err,exc", [
    (McpError("x", status=401), AuthRequired),
    (McpError("x", status=403), PermissionDenied),
    (McpError("x", status=500), UpstreamError),
    (ToolError("tool said no"), UpstreamError),
    (RuntimeError("secret-token-abc"), UpstreamError),
])
async def test_error_mapping(err, exc):
    b = McpBackend(FakeSession(error=err), SITE)
    with pytest.raises(exc) as ei:
        await b.chat_poll("c", "t")
    assert "secret-token-abc" not in str(ei.value)


async def test_rate_limited_retry_after():
    b = McpBackend(FakeSession(error=McpError("x", status=429, retry_after=7.0)), SITE)
    with pytest.raises(RateLimited) as ei:
        await b.chat_send("hi")
    assert ei.value.retry_after == 7.0
