import logging

import pytest
from mautrix.util.async_db import Database

from bitsai.backend.base import (
    ChatStatus,
    ChatTurn,
    PendingAction,
    PermissionDenied,
)
from bitsai.db import upgrade_table
from bitsai.repos import ConversationRepo, RoomSettingsRepo
from bitsai.services.chat import ChatService, build_context_message
from bitsai.sites import Site
from tests.fakes import FakeFactory, FakeMessenger

R, T = "!r:x", "$root"
ALICE, BOB = "@alice:x", "@bob:x"


class Backend:
    def __init__(self, sends=(), polls=()):
        self.sends = list(sends)
        self.polls = list(polls)
        self.send_calls = []
        self.poll_calls = 0

    async def chat_send(self, message, conversation_id=None, pending_action_id=None):
        self.send_calls.append((message, conversation_id, pending_action_id))
        r = self.sends.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def chat_poll(self, conversation_id, task_id):
        self.poll_calls += 1
        r = self.polls.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def turn(status, **kw):
    return ChatTurn("c1", "t1", status, **kw)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
async def db():
    d = Database.create("sqlite::memory:", upgrade_table=upgrade_table, log=logging.getLogger("t"))
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
def env(db):
    clock = Clock()
    sleeps = []

    async def sleep(s):
        sleeps.append(s)
        clock.now += s

    m = FakeMessenger()
    backends = {}
    svc = ChatService(
        FakeFactory(backends),
        m,
        ConversationRepo(db),
        Site("datadoghq.com"),
        logging.getLogger("t"),
        timeout_s=10,
        clock=clock,
        sleep=sleep,
        settings=RoomSettingsRepo(db),
    )
    return svc, m, backends, clock, sleeps


async def test_happy_path(env):
    svc, m, backends, _, _ = env
    backends[ALICE] = Backend(
        [turn(ChatStatus.QUEUED)],
        [turn(ChatStatus.RUNNING), turn(ChatStatus.COMPLETED, message="See [m](/monitors/1)")],
    )
    await svc.ask(R, T, ALICE, "hi")
    assert len(m.sent) == 1 and m.sent[0].thread_root == T
    assert len(m.edits) == 1
    assert "https://app.datadoghq.com/monitors/1" in m.edits[0][2].formatted_body
    assert m.typing == [(R, True), (R, False)]


async def test_followup_reuses_conversation(env):
    svc, m, backends, _, _ = env
    b = backends[ALICE] = Backend(
        [turn(ChatStatus.COMPLETED, message="a"), turn(ChatStatus.COMPLETED, message="b")]
    )
    await svc.ask(R, T, ALICE, "one")
    await svc.ask(R, T, ALICE, "two")
    assert b.send_calls == [("one", None, None), ("two", "c1", None)]


def _two(env, bob_turns=None):
    svc, m, backends, _, _ = env
    a = backends[ALICE] = Backend(
        [
            turn(ChatStatus.COMPLETED, message="a1"),
            turn(ChatStatus.COMPLETED, message="a2"),
        ]
    )
    b = backends[BOB] = Backend(
        bob_turns
        or [
            ChatTurn("cb", "t", ChatStatus.COMPLETED, message="b1"),
            ChatTurn("cb", "t", ChatStatus.COMPLETED, message="b2"),
        ]
    )
    return svc, m, a, b


async def test_second_user_gets_own_conversation_with_context(env):
    svc, m, a, b = _two(env)
    await svc.ask(R, T, ALICE, "alice q")
    m.thread_messages[T] = [
        (ALICE, "alice q"),
        ("@bot:x", "Bits AI is thinking…"),
        ("@bot:x", "a1"),
        (BOB, "bob q"),
    ]
    await svc.ask(R, T, BOB, "bob q")
    assert svc.factory.calls == [ALICE, BOB]
    assert len(a.send_calls) == 1
    msg, cid, _ = b.send_calls[0]
    assert cid is None
    assert msg.startswith("Context: the following is a shared Matrix thread")
    assert f"{ALICE}: alice q" in msg and "@bot:x: a1" in msg
    assert "thinking" not in msg
    assert msg.count("bob q") == 1 and msg.endswith("My question: bob q")
    # bob's answer is prefixed with the asker and the placeholder names him
    assert "Bits AI is thinking (for @bob:x)" in m.sent[-1].content.body
    assert m.edits[-1][2].body.startswith("**→ @bob:x**")
    # follow-ups continue each person's own conversation, without re-sending context
    await svc.ask(R, T, BOB, "bob again")
    await svc.ask(R, T, ALICE, "alice again")
    assert b.send_calls[1] == ("bob again", "cb", None)
    assert a.send_calls[1] == ("alice again", "c1", None)


async def test_context_disabled_sends_plain(env, db):
    svc, m, a, b = _two(env)
    await svc.ask(R, T, ALICE, "alice q")
    await RoomSettingsRepo(db).set_thread_context(R, False)
    m.thread_messages[T] = [(ALICE, "alice q"), (BOB, "bob q")]
    await svc.ask(R, T, BOB, "bob q")
    assert b.send_calls[0] == ("bob q", None, None)


async def test_no_context_when_thread_is_fresh(env):
    svc, m, backends, _, _ = env
    b = backends[ALICE] = Backend([turn(ChatStatus.COMPLETED, message="a")])
    m.thread_messages[T] = [(ALICE, "hi")]
    await svc.ask(R, T, ALICE, "hi")
    assert b.send_calls[0] == ("hi", None, None)
    assert "→" not in m.edits[-1][2].body


async def test_users_not_blocked_by_each_other(env):
    import asyncio

    svc, m, backends, _, _ = env
    gate = asyncio.Event()

    class Slow(Backend):
        async def chat_send(self, *a, **k):
            await gate.wait()
            return await super().chat_send(*a, **k)

    backends[ALICE] = Slow([turn(ChatStatus.COMPLETED, message="a")])
    backends[BOB] = Backend([ChatTurn("cb", "t", ChatStatus.COMPLETED, message="b")])
    t = asyncio.create_task(svc.ask(R, T, ALICE, "one"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await svc.ask(R, T, BOB, "two")
    assert not any("Still working" in x.content.body for x in m.sent)
    assert len(backends[BOB].send_calls) == 1
    gate.set()
    await t
    assert len(backends[ALICE].send_calls) == 1


def test_context_truncation_keeps_most_recent():
    msgs = [(ALICE, f"line{i} " + "x" * 500) for i in range(40)]
    out = build_context_message(msgs, BOB, "q")
    assert "[earlier messages omitted]" in out
    assert "line39" in out and "line0 " not in out
    preamble = out.split("\n---\n")[1]
    assert len(preamble) <= 6000


def test_context_truncation_single_huge_line():
    out = build_context_message([(ALICE, "a" * 20000 + "END")], BOB, "q")
    assert "END" in out and len(out.split("\n---\n")[1]) <= 6000


def test_context_excludes_trigger_and_notices():
    msgs = [(ALICE, "hello"), ("@bot:x", "⚠️ boom"), (BOB, "q")]
    out = build_context_message(msgs, BOB, "q")
    assert "boom" not in out
    assert f"{BOB}:" not in out
    assert build_context_message([(BOB, "q")], BOB, "q") is None


async def test_unlinked(env):
    svc, m, _, _, _ = env
    await svc.ask(R, T, ALICE, "hi")
    assert m.login_needed == [(R, ALICE)]
    assert m.sent == [] and m.typing == []


async def _pending(env):
    svc, m, backends, clock, _ = env
    b = backends[ALICE] = Backend(
        [
            turn(ChatStatus.REQUIRES_ACTION, pending_action=PendingAction("pa1", "Do **it**")),
            turn(ChatStatus.COMPLETED, message="done"),
        ]
    )
    backends[BOB] = Backend()
    await svc.ask(R, T, ALICE, "act")
    return b


async def test_requires_action_flow(env):
    svc, m, backends, _, _ = env
    b = await _pending(env)
    ph = m.sent[0].event_id
    assert [r[2] for r in m.reactions] == ["✅", "❌"]
    assert "approval" in m.edits[0][2].body
    assert await svc.on_reaction(R, "$other", ALICE, "✅") is False
    assert await svc.on_reaction(R, ph, BOB, "✅") is True
    assert len(b.send_calls) == 1
    assert backends[BOB].send_calls == []
    assert await svc.on_reaction(R, ph, ALICE, "✅") is True
    assert b.send_calls[1] == ("Approved.", "c1", "pa1")
    assert m.sent[1].thread_root == T
    assert m.edits[-1][2].body == "done"
    # single use
    assert await svc.on_reaction(R, ph, ALICE, "✅") is False
    assert len(b.send_calls) == 2


async def test_deny(env):
    svc, m, _, _, _ = env
    b = await _pending(env)
    await svc.on_reaction(R, m.sent[0].event_id, ALICE, "❌")
    assert b.send_calls[1][0] == "Denied."


async def test_pending_expires(env):
    svc, m, _, clock, _ = env
    b = await _pending(env)
    clock.now += 16 * 60
    assert await svc.on_reaction(R, m.sent[0].event_id, ALICE, "✅") is False
    assert len(b.send_calls) == 1


async def test_timeout(env):
    svc, m, backends, _, sleeps = env
    backends[ALICE] = Backend([turn(ChatStatus.QUEUED)], [turn(ChatStatus.RUNNING)] * 20)
    await svc.ask(R, T, ALICE, "slow")
    assert "too long" in m.edits[-1][2].body
    assert sleeps and min(sleeps) >= 1
    assert m.typing[-1] == (R, False)


async def test_backend_error_rendered(env):
    svc, m, backends, _, _ = env
    backends[ALICE] = Backend([PermissionDenied("Missing assistant_access")])
    await svc.ask(R, T, ALICE, "x")
    assert "Missing assistant_access" in m.edits[-1][2].body
    assert m.typing[-1] == (R, False)


async def test_unexpected_error_generic(env):
    svc, m, backends, _, _ = env
    backends[ALICE] = Backend([RuntimeError("secret-token-xyz")])
    await svc.ask(R, T, ALICE, "x")
    assert "secret-token-xyz" not in m.edits[-1][2].body
    assert "went wrong" in m.edits[-1][2].body
    assert m.typing[-1] == (R, False)


async def test_busy_guard(env):
    import asyncio

    svc, m, backends, _, _ = env
    gate = asyncio.Event()

    class Slow(Backend):
        async def chat_send(self, *a, **k):
            await gate.wait()
            return await super().chat_send(*a, **k)

    backends[ALICE] = Slow([turn(ChatStatus.COMPLETED, message="a")])
    t = asyncio.create_task(svc.ask(R, T, ALICE, "one"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await svc.ask(R, T, ALICE, "two")
    assert "Still working" in m.sent[-1].content.body
    gate.set()
    await t
    assert m.edits[-1][2].body == "a"
