import logging

import pytest
from mautrix.util.async_db import Database

from bitsai.backend.base import PermissionDenied, ResourceKind, ResourceSummary, UpstreamError
from bitsai.db import upgrade_table
from bitsai.repos import RoomSettingsRepo
from bitsai.services.unfurl import UnfurlService, find_links
from bitsai.sites import Site
from tests.fakes import FakeFactory, FakeMessenger

US = Site("datadoghq.com")
EU = Site("datadoghq.eu")
K = ResourceKind


@pytest.mark.parametrize(
    "text,site,expected",
    [
        ("https://app.datadoghq.com/monitors/123?x=1#f", US, [(K.MONITOR, "123")]),
        (
            "see https://app.datadoghq.com/dashboard/abc-def-ghi/my-slug.",
            US,
            [(K.DASHBOARD, "abc-def-ghi")],
        ),
        ("https://app.datadoghq.com/incidents/7", US, [(K.INCIDENT, "7")]),
        ("https://app.datadoghq.com/notebook/42", US, [(K.NOTEBOOK, "42")]),
        ("https://acme.datadoghq.com/monitors/5", US, [(K.MONITOR, "5")]),
        ("https://app.datadoghq.eu/monitors/9", EU, [(K.MONITOR, "9")]),
        ("https://app.datadoghq.eu/monitors/9", US, []),
        ("https://app.datadoghq.com/monitors/9", EU, []),
        ("https://app.us3.datadoghq.com/monitors/9", US, []),
        ("https://us3.datadoghq.com/monitors/9", US, []),
        ("https://api.datadoghq.com/monitors/9", US, []),
        ("https://mcp.datadoghq.com/monitors/9", US, []),
        ("http://app.datadoghq.com/monitors/9", US, []),
        ("https://user@app.datadoghq.com/monitors/9", US, []),
        ("https://app.datadoghq.com.evil.com/monitors/9", US, []),
        ("https://evildatadoghq.com/monitors/9", US, []),
        ("https://app.datadoghq.com/monitors/abc", US, []),
        ("https://app.datadoghq.com/dashboard/12/x", US, []),
        ("https://app.datadoghq.com/logs?x=1", US, []),
        (
            "https://app.datadoghq.com/monitors/1 https://app.datadoghq.com/monitors/1 "
            "https://app.datadoghq.com/monitors/2 https://app.datadoghq.com/monitors/3 "
            "https://app.datadoghq.com/monitors/4",
            US,
            [(K.MONITOR, "1"), (K.MONITOR, "2"), (K.MONITOR, "3")],
        ),
    ],
)
def test_find_links(text, site, expected):
    assert find_links(text, site) == expected


class Backend:
    def __init__(self, deny=()):
        self.calls = []
        self.deny = set(deny)

    async def resource_get(self, kind, rid):
        self.calls.append((kind, rid))
        if rid in self.deny:
            raise PermissionDenied("no")
        if rid == "404":
            raise UpstreamError("nf")
        return ResourceSummary(kind, rid, f"M{rid}", f"https://app.datadoghq.com/monitors/{rid}")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
async def db():
    d = Database.create("sqlite::memory:", upgrade_table=upgrade_table, log=logging.getLogger("t"))
    await d.start()
    yield d
    await d.stop()


def url(i):
    return f"https://app.datadoghq.com/monitors/{i}"


def make(db, backends, **kw):
    msg = FakeMessenger()
    clock = Clock()
    svc = UnfurlService(
        FakeFactory(backends), msg, RoomSettingsRepo(db), US, logging.getLogger("t"),
        clock=clock, **kw,
    )
    return svc, msg, clock


async def test_disabled_no_calls(db):
    b = Backend()
    svc, msg, _ = make(db, {"@a": b})
    await svc.maybe_unfurl("!r", "$e", "@a", url(1))
    assert not b.calls and not msg.sent and not svc.factory.calls


async def test_enabled_posts_reply(db):
    b = Backend()
    svc, msg, _ = make(db, {"@a": b})
    await svc.set_enabled("!r", True)
    await svc.maybe_unfurl("!r", "$e", "@a", f"{url(1)} {url(2)}", thread_root="$t")
    assert [s.reply_to for s in msg.sent] == ["$e", "$e"]
    assert all(s.thread_root == "$t" for s in msg.sent)
    assert "M1" in msg.sent[0].content.body


async def test_default_enabled(db):
    b = Backend()
    svc, msg, _ = make(db, {"@a": b}, default_enabled=True)
    await svc.maybe_unfurl("!r", "$e", "@a", url(1))
    assert len(msg.sent) == 1
    await svc.set_enabled("!r", False)
    await svc.maybe_unfurl("!r", "$e", "@a", url(2))
    assert len(msg.sent) == 1


async def test_unlinked_silent(db):
    svc, msg, _ = make(db, {})
    await svc.set_enabled("!r", True)
    await svc.maybe_unfurl("!r", "$e", "@a", url(1))
    assert not msg.sent and not msg.login_needed


async def test_denied_and_errors_skipped(db):
    b = Backend(deny={"1"})
    svc, msg, _ = make(db, {"@a": b})
    await svc.set_enabled("!r", True)
    await svc.maybe_unfurl("!r", "$e", "@a", f"{url(1)} {url(404)} {url(3)}")
    assert len(msg.sent) == 1 and "M3" in msg.sent[0].content.body


async def test_cache_keyed_by_sender(db):
    a, b = Backend(), Backend()
    svc, msg, clock = make(db, {"@a": a, "@b": b})
    await svc.set_enabled("!r", True)
    await svc.maybe_unfurl("!r", "$e1", "@a", url(1))
    await svc.maybe_unfurl("!r", "$e2", "@a", url(1))
    assert len(a.calls) == 1 and len(msg.sent) == 2
    await svc.maybe_unfurl("!r", "$e3", "@b", url(1))
    assert len(b.calls) == 1
    clock.t = 100
    await svc.maybe_unfurl("!r", "$e4", "@a", url(1))
    assert len(a.calls) == 2


async def test_rate_cap(db):
    svc, msg, clock = make(db, {"@a": Backend()})
    await svc.set_enabled("!r", True)
    for i in range(8):
        await svc.maybe_unfurl("!r", f"$e{i}", "@a", url(i + 1))
    assert len(msg.sent) == 5
    clock.t = 61
    await svc.maybe_unfurl("!r", "$x", "@a", url(20))
    assert len(msg.sent) == 6
