import asyncio
import json
import logging

import pytest
from mautrix.util.async_db import Database

from bitsai.backend.base import MonitorAlertRef
from bitsai.db import upgrade_table
from bitsai.repos import AlertMessageRepo, RoomSettingsRepo
from bitsai.services.alerts import AlertService, parse_payload
from bitsai.sites import Site
from tests.fakes import FakeMessenger

ROOM = "!r:x"
EID = "1234567890123456789"


@pytest.fixture
async def db():
    d = Database.create("sqlite::memory:", upgrade_table=upgrade_table, log=logging.getLogger("t"))
    await d.start()
    yield d
    await d.stop()


class Env:
    def __init__(self, db):
        self.m = FakeMessenger()
        self.settings = RoomSettingsRepo(db)
        self.alerts = AlertMessageRepo(db)
        self.inv = []
        self.ask = []
        self.t = [1000.0]

        async def on_inv(*a):
            self.inv.append(a)

        async def on_ask(*a):
            self.ask.append(a)

        self.svc = AlertService(
            self.m,
            self.settings,
            self.alerts,
            Site("datadoghq.com"),
            logging.getLogger("t"),
            on_investigate=on_inv,
            on_ask=on_ask,
            clock=lambda: self.t[0],
        )

    async def drain(self):
        await asyncio.gather(*list(self.svc._tasks))


@pytest.fixture
async def env(db):
    e = Env(db)
    yield e
    await e.svc.stop()


def body(**kw):
    d = {
        "alert_id": "42",
        "monitor_id": "42",
        "event_id": EID,
        "event_ts": "1700000000000",
        "title": "CPU high",
        "status": "Alert",
        "transition": "Triggered",
        "tags": "env:prod,svc:web",
    }
    d.update(kw)
    return json.dumps(d).encode()


async def test_token_hashed_and_rotation(env, db):
    tok, text = await env.svc.create_webhook(ROOM, "https://h.example/base")
    assert f"https://h.example/base/hook/{tok}" in text
    assert "$ALERT_ID" in text and "secret" in text
    rows = await db.fetch("SELECT * FROM room_settings")
    assert tok not in str([tuple(r) for r in rows])
    tok2, _ = await env.svc.create_webhook(ROOM, "https://h")
    assert await env.svc.handle_webhook(tok, body()) == 404
    assert await env.svc.handle_webhook(tok2, body()) == 202
    await env.svc.revoke_webhook(ROOM)
    assert await env.svc.handle_webhook(tok2, body()) == 404


async def test_unknown_and_bad(env):
    assert await env.svc.handle_webhook("nope", b"{}") == 404
    assert await env.svc.handle_webhook("A" * 43, b"{}") == 404
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    assert await env.svc.handle_webhook(tok, b"x" * 70_000) == 400
    assert await env.svc.handle_webhook(tok, b"not json") == 400
    assert await env.svc.handle_webhook(tok, b"[1]") == 400


async def test_post_and_reactions_and_injection(env):
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    assert await env.svc.handle_webhook(tok, body(title="<script>x</script> **b**")) == 202
    await env.drain()
    sent = env.m.sent[0]
    assert "<script>" not in sent.content.formatted_body
    assert "&lt;script&gt;" in sent.content.formatted_body
    assert [r[2] for r in env.m.reactions] == ["🔍", "💬"]
    assert await env.alerts.get_by_event(sent.event_id) is not None


async def test_dedupe(env):
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    assert await env.svc.handle_webhook(tok, body()) == 202
    await env.drain()
    assert await env.svc.handle_webhook(tok, body()) == 202
    await env.drain()
    assert len(env.m.sent) == 1
    env.t[0] += 700
    assert await env.svc.handle_webhook(tok, body(event_id=str(int(EID) + 1))) == 202
    await env.drain()
    assert len(env.m.sent) == 2


async def test_rate_limit(env):
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    codes = [await env.svc.handle_webhook(tok, body(alert_id=str(i))) for i in range(31)]
    assert codes[:30] == [202] * 30 and codes[30] == 429
    env.t[0] += 61
    assert await env.svc.handle_webhook(tok, body(alert_id="new")) == 202


async def test_reactions(env):
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    await env.svc.handle_webhook(tok, body())
    await env.drain()
    eid = env.m.sent[0].event_id
    assert await env.svc.on_reaction(ROOM, eid, "@u:x", "🔍", bot_mxid="@bot:x")
    assert env.inv == [(ROOM, eid, "@u:x", MonitorAlertRef(42, EID, 1700000000000))]
    assert await env.svc.on_reaction(ROOM, eid, "@u:x", "💬")
    assert env.ask[0][:3] == (ROOM, eid, "@u:x") and "42" in env.ask[0][3]
    assert not await env.svc.on_reaction(ROOM, eid, "@bot:x", "🔍", bot_mxid="@bot:x")
    assert not await env.svc.on_reaction(ROOM, "$other", "@u:x", "🔍")
    assert not await env.svc.on_reaction(ROOM, eid, "@u:x", "👍")
    assert not await env.svc.on_reaction("!other:x", eid, "@u:x", "🔍")


async def test_missing_event_data(env):
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    await env.svc.handle_webhook(tok, body(event_id="123"))
    await env.drain()
    # no alert row stored -> reaction not handled
    assert not await env.svc.on_reaction(ROOM, env.m.sent[0].event_id, "@u:x", "🔍")
    assert env.inv == []
    # row with zeroed data -> in-thread reply
    await env.alerts.add("$z", ROOM, 0, "", 0, "k")
    assert await env.svc.on_reaction(ROOM, "$z", "@u:x", "🔍")
    assert env.m.sent[-1].thread_root == "$z" and "lacks" in env.m.sent[-1].content.body
    assert env.inv == []


def test_parse_payload():
    p = parse_payload(
        json.dumps(
            {
                "title": "t" * 1000,
                "monitor_id": 7,
                "event_id": "12",
                "event_ts": "2023-11-14T22:13:20Z",
                "tags": "a, b",
                "evil": "x",
            }
        ).encode()
    )
    assert len(p["title"]) == 300 and p["monitor_id"] == 7
    assert "event_id" not in p and "evil" not in p
    assert p["event_ts"] == 1700000000000 and p["tags"] == ["a", "b"]
    assert parse_payload(b'{"event_ts": 1700000000}')["event_ts"] == 1700000000000


async def test_stop_cancels_tasks(env):
    gate = asyncio.Event()

    async def slow(*a, **k):
        await gate.wait()

    env.m.send = slow
    tok, _ = await env.svc.create_webhook(ROOM, "https://h")
    await env.svc.handle_webhook(tok, body())
    await asyncio.sleep(0)
    assert env.svc._tasks
    await env.svc.stop()
    assert not env.svc._tasks
