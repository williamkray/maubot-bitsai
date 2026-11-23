import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitsai.backend.base import (
    AuthRequired,
    InvestigationStatus,
    MonitorAlertRef,
    PermissionDenied,
    RateLimited,
    ResourceKind,
    UpstreamError,
)
from bitsai.backend.rest import RestBackend
from bitsai.sites import Site

SITE = Site("datadoghq.com", "acme")


class Redirect(aiohttp.abc.AbstractResolver):
    """Resolve api.datadoghq.com to the local test server."""

    def __init__(self, port):
        self.port = port

    async def resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "127.0.0.1", "port": self.port, "family": 2,
                 "proto": 0, "flags": 0}]

    async def close(self):
        pass


class Harness:
    def __init__(self, routes):
        self.requests = []
        self.routes = routes  # (method, path) -> (status, body, headers)

    async def handler(self, request):
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, dict(request.query), body,
                              request.headers.get("Authorization")))
        status, payload, headers = self.routes.get(
            (request.method, request.path), (404, {"errors": ["nf"]}, {}))
        return web.json_response(payload, status=status, headers=headers)

    async def __aenter__(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handler)
        self.server = TestServer(app)
        await self.server.start_server()
        conn = aiohttp.TCPConnector(resolver=Redirect(self.server.port), ssl=False)
        self.http = aiohttp.ClientSession(connector=conn)
        return self

    async def __aexit__(self, *a):
        await self.http.close()
        await self.server.close()

    def backend(self, token="tok"):
        # api_url is https; the redirecting resolver + ssl=False needs plain http, so patch.
        b = RestBackend(self.http, _HttpSite(), lambda: token)
        return b


class _HttpSite(Site):
    def __init__(self):
        super().__init__("datadoghq.com", "acme")

    @property
    def api_url(self):
        return "http://api.datadoghq.com"


def ok(fx):
    return {(fx["request"]["method"], fx["request"]["path"]): (fx["status"], fx["body"], {})}


async def test_start_general_body_matches_fixture(load_fixture):
    fx = load_fixture("investigation_trigger.json")
    async with Harness(ok(fx)) as h:
        iid = await h.backend().investigation_start_general(
            fx["request"]["body"]["data"]["attributes"]["trigger"]["general_investigation"][
                "description"])
    assert iid == fx["body"]["data"]["attributes"]["investigation_id"]
    method, path, _, body, auth = h.requests[0]
    assert body == fx["request"]["body"] and auth == "Bearer tok"


async def test_start_from_alert_body(load_fixture):
    fx = load_fixture("investigation_trigger.json")
    async with Harness(ok(fx)) as h:
        await h.backend().investigation_start_from_alert(MonitorAlertRef(7, "1" * 16, 123))
    assert h.requests[0][3] == {"data": {"type": "trigger_investigation_request", "attributes": {
        "trigger": {"type": "monitor_alert_trigger", "monitor_alert_trigger": {
            "monitor_id": 7, "event_id": "1" * 16, "event_ts": 123}}}}}


async def test_investigation_get(load_fixture):
    for name, status, nconcl in (("investigation_get_in_progress.json",
                                  InvestigationStatus.IN_PROGRESS, 0),
                                 ("investigation_get_conclusive.json",
                                  InvestigationStatus.CONCLUSIVE, 1)):
        fx = load_fixture(name)
        async with Harness(ok(fx)) as h:
            inv = await h.backend().investigation_get(fx["body"]["data"]["id"])
        assert inv.status is status and len(inv.conclusions) == nconcl
        assert inv.url == fx["body"]["links"]["self"]


async def test_relative_link_made_absolute():
    body = {"data": {"id": "i", "attributes": {"status": "mystery", "conclusions": []}},
            "links": {"self": "/bits-ai/investigations/i"}}
    async with Harness({("GET", "/api/v2/bits-ai/investigations/i"): (200, body, {})}) as h:
        inv = await h.backend().investigation_get("i")
    assert inv.url == "https://acme.datadoghq.com/bits-ai/investigations/i"
    assert inv.status is InvestigationStatus.UNKNOWN and inv.raw_status == "mystery"


async def test_resource_get(load_fixture):
    routes = {}
    for n in ("getter_monitor.json", "getter_dashboard.json", "getter_notebook.json"):
        routes.update(ok(load_fixture(n)))
    async with Harness(routes) as h:
        b = h.backend()
        m = await b.resource_get(ResourceKind.MONITOR, "100000001")
        d = await b.resource_get(ResourceKind.DASHBOARD, "abc-def-ghi")
        n = await b.resource_get(ResourceKind.NOTEBOOK, "1000")
    assert m.status == "OK" and m.title.startswith("CPU") and m.url.endswith("/monitors/100000001")
    assert d.url == "https://acme.datadoghq.com/dashboard/abc-def-ghi/jmmm-dashboard"
    assert n.title and n.id == "1000"


async def test_incident_get():
    body = {"data": {"id": "uuid1", "attributes": {"title": "DB down", "public_id": 12,
                                                  "state": "active"}}}
    async with Harness({("GET", "/api/v2/incidents/uuid1"): (200, body, {})}) as h:
        r = await h.backend().resource_get(ResourceKind.INCIDENT, "uuid1")
    assert r.title == "DB down" and r.status == "active" and r.url.endswith("/incidents/12")


async def test_graph_snapshot(load_fixture):
    fx = load_fixture("snapshot.json")
    async with Harness(ok(fx)) as h:
        url = await h.backend().graph_snapshot("avg:system.cpu.user{*}", 1, 2)
    assert url == fx["body"]["snapshot_url"]
    assert h.requests[0][2] == {"metric_query": "avg:system.cpu.user{*}", "start": "1", "end": "2"}


@pytest.mark.parametrize("status,exc", [(401, AuthRequired), (403, PermissionDenied),
                                        (429, RateLimited), (500, UpstreamError),
                                        (404, UpstreamError)])
async def test_error_mapping(status, exc):
    routes = {("GET", "/api/v1/monitor/1"): (status, {"errors": ["secret"]},
                                             {"Retry-After": "3"})}
    async with Harness(routes) as h:
        with pytest.raises(exc) as ei:
            await h.backend().resource_get(ResourceKind.MONITOR, "1")
    assert "secret" not in str(ei.value)
    if exc is RateLimited:
        assert ei.value.retry_after == 3.0


async def test_unsupported_methods():
    async with Harness({}) as h:
        b = h.backend()
        for coro in (b.chat_send("x"), b.chat_poll("a", "b"), b.investigation_steer("a", "b"),
                     b.investigation_resume("a", "b")):
            with pytest.raises(UpstreamError, match="not supported over REST"):
                await coro


async def test_missing_token_and_async_getter():
    async with Harness({}) as h:
        with pytest.raises(AuthRequired):
            await RestBackend(h.http, _HttpSite(), lambda: None).graph_snapshot("q", 1, 2)

        async def getter():
            return "t2"

        routes_h = Harness({("GET", "/api/v1/graph/snapshot"): (200, {"snapshot_url": "u"}, {})})
    async with routes_h as h2:
        b = RestBackend(h2.http, _HttpSite(), getter)
        assert await b.graph_snapshot("q", 1, 2) == "u"
        assert h2.requests[0][4] == "Bearer t2"
