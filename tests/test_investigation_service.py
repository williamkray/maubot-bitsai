import asyncio
import logging

import pytest
from mautrix.util.async_db import Database

from bitsai.backend.base import (
    AuthRequired,
    Conclusion,
    Investigation,
    InvestigationStatus,
    MonitorAlertRef,
    UpstreamError,
)
from bitsai.db import upgrade_table
from bitsai.repos import InvestigationRepo
from bitsai.services.investigations import InvestigationService
from bitsai.sites import Site
from tests.fakes import FakeFactory, FakeMessenger

ROOM, THREAD = "!r:x", "$t"
ALICE, BOB = "@alice:x", "@bob:x"


def inv(status, raw=None, **kw):
    return Investigation("i1", InvestigationStatus.parse(status), raw or status, **kw)


class FakeBackend:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def investigation_start_general(self, description, **kw):
        self.calls.append(("start", description))
        return "i1"

    async def investigation_start_from_alert(self, alert):
        self.calls.append(("alert", alert.monitor_id))
        return "i1"

    async def investigation_get(self, iid):
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def investigation_steer(self, iid, message):
        self.calls.append(("steer", message))

    async def investigation_resume(self, iid, context):
        self.calls.append(("resume", context))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.t += d
        await asyncio.sleep(0)


@pytest.fixture
async def db():
    d = Database.create("sqlite::memory:", upgrade_table=upgrade_table, log=logging.getLogger("t"))
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
def env(db):
    clock = Clock()
    m = FakeMessenger()
    repo = InvestigationRepo(db)

    def make(backends, **kw):
        f = FakeFactory(backends)
        kw.setdefault("sleep", clock.sleep)
        svc = InvestigationService(
            f, m, repo, Site("datadoghq.com"), logging.getLogger("t"), clock=clock, **kw
        )
        return svc, f

    return make, m, repo, clock


CONCL = inv(
    "conclusive",
    url="https://app.datadoghq.com/i/1",
    conclusions=(Conclusion("Root", "Bad deploy"),),
)


async def test_full_flow(env):
    make, m, repo, _ = env
    be = FakeBackend([inv("in progress"), inv("in progress", title="Checking"), CONCL])
    svc, f = make({ALICE: be})
    assert await svc.start_general(ROOM, THREAD, ALICE, "why 500s") == "i1"
    assert m.sent[0].thread_root == THREAD
    await svc.join()
    assert "Bad deploy" in m.sent[-1].content.body
    assert m.sent[-1].thread_root == THREAD
    assert m.edits and "conclusive" in m.edits[-1][2].body
    row = await repo.get("i1")
    assert row.finished_at and row.last_status == "conclusive"
    assert row.requested_by == ALICE


async def test_from_alert(env):
    make, m, repo, _ = env
    be = FakeBackend([CONCL])
    svc, _ = make({ALICE: be})
    assert await svc.start_from_alert(ROOM, THREAD, ALICE, MonitorAlertRef(5, "1" * 16, 1)) == "i1"
    await svc.join()
    assert ("alert", 5) in be.calls


async def test_unknown_status_keeps_running(env):
    make, m, repo, _ = env
    be = FakeBackend([inv("weird"), inv("weird"), CONCL])
    svc, _ = make({ALICE: be})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    assert (await repo.get("i1")).last_status == "conclusive"


async def test_transient_errors_continue(env):
    make, m, repo, _ = env
    be = FakeBackend([UpstreamError("blip"), CONCL])
    svc, _ = make({ALICE: be})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    assert (await repo.get("i1")).finished_at


async def test_timeout(env):
    make, m, repo, _ = env
    svc, _ = make(
        {ALICE: FakeBackend([inv("in progress", url="https://app.datadoghq.com/i/1")])},
        max_minutes=2,
    )
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    row = await repo.get("i1")
    assert row.finished_at and row.last_status == "in progress"
    assert "still running" in m.edits[-1][2].body
    assert "https://app.datadoghq.com/i/1" in m.edits[-1][2].body


async def test_unlinked_user(env):
    make, m, repo, _ = env
    svc, _ = make({})
    assert await svc.start_general(ROOM, THREAD, ALICE, "x") is None
    assert m.login_needed == [(ROOM, ALICE)]
    assert await repo.list_open() == []


async def test_description_validation(env):
    make, m, _, _ = env
    svc, f = make({ALICE: FakeBackend([CONCL])})
    assert await svc.start_general(ROOM, THREAD, ALICE, "  ") is None
    assert await svc.start_general(ROOM, THREAD, ALICE, "a" * 4097) is None
    assert len(m.sent) == 2 and f.calls == []


async def test_steer_non_owner_refused(env):
    make, m, _, _ = env
    be, bob = FakeBackend([inv("in progress")]), FakeBackend([inv("in progress")])
    svc, f = make({ALICE: be, BOB: bob}, max_minutes=1000)
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.steer(ROOM, THREAD, BOB, "look at db")
    assert "Only the person" in m.sent[-1].content.body
    assert not any(c[0] == "steer" for c in be.calls + bob.calls)
    await svc.stop()


async def test_steer_running_and_per_poll_requester(env):
    make, m, _, _ = env
    be, bob = FakeBackend([inv("in progress")]), FakeBackend([CONCL])
    svc, f = make({ALICE: be, BOB: bob}, max_minutes=1000)
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.steer(ROOM, THREAD, ALICE, "look at db")
    assert ("steer", "look at db") in be.calls
    await svc.stop()
    assert BOB not in f.calls


async def test_resume_terminal(env):
    make, m, repo, _ = env
    be = FakeBackend([CONCL])
    svc, _ = make({ALICE: be})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    assert (await repo.get("i1")).finished_at
    await svc.steer(ROOM, THREAD, ALICE, "also check cache")
    assert ("resume", "also check cache") in be.calls
    await svc.join()  # re-polls to conclusive again
    assert (await repo.get("i1")).finished_at


async def test_resume_open(env):
    make, m, repo, clock = env
    now = int(clock())
    await repo.add("fresh", ROOM, "$a", ALICE, status_event_id="$s1", started_at=now - 60)
    await repo.add("stale", ROOM, "$b", ALICE, status_event_id="$s2", started_at=now - 10_000)
    be = FakeBackend([Investigation("fresh", InvestigationStatus.CONCLUSIVE, "conclusive")])
    svc, _ = make({ALICE: be})
    await svc.resume_open()
    await svc.join()
    assert (await repo.get("fresh")).last_status == "conclusive"
    stale = await repo.get("stale")
    assert stale.finished_at
    assert "still running" in [e for e in m.edits if e[1] == "$s2"][0][2].body


async def test_auth_required_mid_poll(env):
    make, m, repo, _ = env
    be = FakeBackend([AuthRequired("expired")])
    svc, _ = make({ALICE: be})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    assert "log in again" in m.sent[-1].content.body
    assert (await repo.get("i1")).finished_at is None
    n = len(m.sent)
    await svc.resume_open()  # would restart; posts the notice once more only on a new poll
    await svc.join()
    assert len(m.sent) == n + 1


async def test_stop_cancels(env):
    make, m, repo, _ = env
    gate = asyncio.Event()

    async def blocked(_):
        await gate.wait()

    svc, _ = make({ALICE: FakeBackend([inv("in progress")])}, sleep=blocked)
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await asyncio.sleep(0)
    assert svc._tasks
    await svc.stop()
    assert not svc._tasks
    assert (await repo.get("i1")).finished_at is None


async def test_failed_status_edit_does_not_stop_polling(env):
    make, m, repo, _ = env

    async def broken_edit(room_id, event_id, content):
        raise RuntimeError("homeserver 502")

    m.edit = broken_edit
    be = FakeBackend([inv("in progress"), inv("in progress", title="Checking"), CONCL])
    svc, _ = make({ALICE: be})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    assert "Bad deploy" in m.sent[-1].content.body
    assert (await repo.get("i1")).finished_at


async def test_failed_result_post_is_retried(env):
    make, m, repo, _ = env
    real_send, failures = m.send, []

    async def flaky_send(room_id, content, **kw):
        if "Bad deploy" in content.body and not failures:
            failures.append(1)
            raise RuntimeError("homeserver 429")
        return await real_send(room_id, content, **kw)

    m.send = flaky_send
    svc, _ = make({ALICE: FakeBackend([CONCL])})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.join()
    assert failures == [1]
    assert sum("Bad deploy" in s.content.body for s in m.sent) == 1
    assert (await repo.get("i1")).finished_at


async def test_no_new_polls_after_stop(env):
    make, m, repo, _ = env
    svc, _ = make({ALICE: FakeBackend([inv("in progress")])})
    await svc.start_general(ROOM, THREAD, ALICE, "x")
    await svc.stop()
    await svc.resume_open()  # e.g. a resume racing with plugin stop
    assert not svc._tasks
    assert (await repo.get("i1")).finished_at is None
