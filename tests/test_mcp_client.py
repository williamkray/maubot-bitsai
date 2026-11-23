from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitsai.mcp.base import McpError, ToolError
from bitsai.mcp.client import McpClient

FIX = Path(__file__).parent / "fixtures"
INIT = json.loads((FIX / "mcp_initialize.json").read_text())
SID = INIT["response_headers"]["Mcp-Session-Id"]
TOKEN = "SECRET-TOKEN-xyz"


class Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.query: list[str] = []
        self.tool_reply = None  # callable(rid) -> web.Response
        self.fail_next: list[web.Response] = []
        self.sid = SID
        self.delay = 0.0

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        self.query.append(request.query_string)
        m = body["method"]
        if m == "initialize":
            return web.json_response(
                {"jsonrpc": "2.0", "id": body["id"], "result": INIT["result"]},
                headers={"Mcp-Session-Id": self.sid})
        if m == "notifications/initialized":
            return web.Response(status=202)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_next:
            return self.fail_next.pop(0)
        if m == "tools/list":
            return web.json_response({"jsonrpc": "2.0", "id": body["id"],
                                      "result": {"tools": [{"name": "x"}]}})
        return self.tool_reply(body["id"])


def sse(rid, result, noise=True):
    msg = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})
    note = '{"jsonrpc":"2.0","method":"notifications/progress"}'
    pre = f"event: message\ndata: {note}\n\n" if noise else ""
    return web.Response(text=f"{pre}event: message\ndata: {msg}\n\n",
                        content_type="text/event-stream")


def text_result(payload):
    return {"content": [{"type": "text", "text": payload}]}


@pytest.fixture
async def env():
    fake = Fake()
    app = web.Application()
    app.router.add_post("/v1/mcp", fake.handle)
    server = TestServer(app)
    await server.start_server()
    calls = {"n": 0}

    async def getter():
        calls["n"] += 1
        return f"{TOKEN}-{calls['n']}"

    log = logging.getLogger("test.mcp")
    async with aiohttp.ClientSession() as http:
        client = McpClient(http, str(server.make_url("/v1/mcp")), getter,
                           ["core", "chat"], log)
        yield fake, client, calls
    await server.close()


async def test_initialize_and_session_echo(env):
    fake, client, _ = env
    assert await client.list_tools() == [{"name": "x"}]
    assert [r["method"] for r in fake.requests] == [
        "initialize", "notifications/initialized", "tools/list"]
    assert fake.requests[0]["params"]["protocolVersion"] == "2025-06-18"
    assert fake.query[0] == "toolsets=core,chat"
    assert "Mcp-Session-Id" not in fake.headers[0]
    assert fake.headers[2]["Mcp-Session-Id"] == SID
    assert fake.headers[2]["MCP-Protocol-Version"] == "2025-06-18"
    ids = [r["id"] for r in fake.requests if "id" in r]
    assert ids == sorted(set(ids))


async def test_sse_and_double_json_decode(env):
    fake, client, _ = env
    inner = {"task_id": "t1", "conversation_id": "c1", "status": "queued"}
    fake.tool_reply = lambda rid: sse(rid, text_result(json.dumps(inner)))
    assert await client.call_tool("send_message_to_chat", {"message": "hi"}) == inner
    fake.tool_reply = lambda rid: sse(rid, text_result("plain words"), noise=False)
    assert await client.call_tool("t", {}) == "plain words"


async def test_json_body_for_tool_call(env):
    fake, client, _ = env
    fake.tool_reply = lambda rid: web.json_response(
        {"jsonrpc": "2.0", "id": rid, "result": text_result("[1, 2]")})
    assert await client.call_tool("t", {}) == [1, 2]


async def test_multiline_sse_data(env):
    fake, client, _ = env

    def reply(rid):
        body = ('data: {"jsonrpc":"2.0",\n'
                f'data: "id":{rid},"result":{{"content":[{{"type":"text","text":"ok"}}]}}}}\n\n')
        return web.Response(text=body, content_type="text/event-stream")

    fake.tool_reply = reply
    assert await client.call_tool("t", {}) == "ok"


async def test_is_error_raises_tool_error(env):
    fake, client, _ = env
    fake.tool_reply = lambda rid: sse(
        rid, {"isError": True, "content": [{"type": "text", "text": "boom"}]})
    with pytest.raises(ToolError, match="boom"):
        await client.call_tool("t", {})


async def test_jsonrpc_error_code(env):
    fake, client, _ = env
    fake.tool_reply = lambda rid: web.json_response(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "bad"}})
    with pytest.raises(McpError) as ei:
        await client.call_tool("t", {})
    assert ei.value.code == -32602 and not isinstance(ei.value, ToolError)


async def test_401(env):
    fake, client, _ = env
    fake.fail_next = [web.Response(status=401, text="nope")]
    with pytest.raises(McpError) as ei:
        await client.call_tool("t", {})
    assert ei.value.status == 401


async def test_429_retry_after(env):
    fake, client, _ = env
    fake.fail_next = [web.Response(status=429, headers={"Retry-After": "7"})]
    with pytest.raises(McpError) as ei:
        await client.call_tool("t", {})
    assert ei.value.status == 429 and ei.value.retry_after == 7.0


async def test_5xx(env):
    fake, client, _ = env
    fake.fail_next = [web.Response(status=503)]
    with pytest.raises(McpError) as ei:
        await client.call_tool("t", {})
    assert ei.value.status == 503


async def test_404_reinit_once(env):
    fake, client, _ = env
    fake.tool_reply = lambda rid: sse(rid, text_result("1"))
    await client.call_tool("t", {})
    fake.sid = "new-session"
    fake.fail_next = [web.Response(status=404)]
    assert await client.call_tool("t", {}) == 1
    methods = [r["method"] for r in fake.requests]
    assert methods.count("initialize") == 2
    assert fake.headers[-1]["Mcp-Session-Id"] == "new-session"
    # second consecutive 404 is not retried forever
    fake.fail_next = [web.Response(status=404), web.Response(status=404)]
    with pytest.raises(McpError) as ei:
        await client.call_tool("t", {})
    assert ei.value.status == 404


async def test_token_getter_per_request_and_parallel(env):
    fake, client, calls = env
    fake.tool_reply = lambda rid: sse(rid, text_result("1"))
    await asyncio.gather(*[client.call_tool("t", {}) for _ in range(5)])
    assert [r["method"] for r in fake.requests].count("initialize") == 1
    assert calls["n"] == len(fake.requests)
    auths = [h["Authorization"] for h in fake.headers]
    assert len(set(auths)) == len(auths)
    rids = [r["id"] for r in fake.requests if r["method"] == "tools/call"]
    assert len(set(rids)) == 5


async def test_timeout(env):
    fake, client, _ = env
    await client.list_tools()  # initialise first
    fake.delay = 1.0
    with pytest.raises(McpError, match="timed out"):
        await client.call_tool("t", {}, timeout=0.05)


async def test_authorization_never_logged(env, caplog):
    fake, client, _ = env
    caplog.set_level(logging.DEBUG)
    fake.tool_reply = lambda rid: sse(rid, text_result("1"))
    await client.call_tool("t", {})
    fake.fail_next = [web.Response(status=401)]
    with pytest.raises(McpError):
        await client.call_tool("t", {})
    assert caplog.records
    for rec in caplog.records:
        assert "SECRET-TOKEN" not in rec.getMessage()
        assert "Bearer" not in rec.getMessage()
