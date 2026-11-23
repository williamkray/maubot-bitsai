import logging

import pytest
from mautrix.util.async_db import Database

from bitsai.db import upgrade_table
from bitsai.repos import (
    AlertMessageRepo,
    ConversationRepo,
    InvestigationRepo,
    RoomSettingsRepo,
)


@pytest.fixture
async def db():
    d = Database.create("sqlite::memory:", upgrade_table=upgrade_table, log=logging.getLogger("t"))
    await d.start()
    yield d
    await d.stop()


async def test_conversation(db):
    r = ConversationRepo(db)
    assert await r.get("!r", "$t", "@u:x") is None
    assert not await r.has_any("!r", "$t")
    await r.put("!r", "$t", "@u:x", "c1")
    await r.put("!r", "$t", "@u:x", "c2")
    assert (await r.get("!r", "$t", "@u:x")).bits_conversation_id == "c2"
    assert await r.get("!r", "$t", "@v:x") is None
    await r.put("!r", "$t", "@v:x", "c3")
    assert (await r.get("!r", "$t", "@u:x")).bits_conversation_id == "c2"
    assert (await r.get("!r", "$t", "@v:x")).bits_conversation_id == "c3"
    assert await r.has_any("!r", "$t")
    assert not await r.has_any("!r", "$other")
    assert sorted(await r.owners("!r", "$t")) == ["@u:x", "@v:x"]


async def test_thread_context_setting(db):
    r = RoomSettingsRepo(db)
    assert (await r.get("!r")).thread_context_enabled is True
    await r.set_thread_context("!r", False)
    assert (await r.get("!r")).thread_context_enabled is False
    await r.set_thread_context("!r", True)
    assert (await r.get("!r")).thread_context_enabled is True


async def test_investigation(db):
    r = InvestigationRepo(db)
    await r.add("i1", "!r", "$t", "@u:x")
    await r.add("i2", "!r", "$t", "@u:x")
    await r.set_status_event("i1", "$e")
    await r.update_status("i1", "needs direction")
    assert {i.investigation_id for i in await r.list_open()} == {"i1", "i2"}
    await r.finish("i1", "conclusive")
    open_ = await r.list_open()
    assert [i.investigation_id for i in open_] == ["i2"]
    done = await r.get("i1")
    assert done.last_status == "conclusive" and done.status_event_id == "$e"
    assert done.finished_at is not None


async def test_room_settings(db):
    r = RoomSettingsRepo(db)
    s = await r.get("!r")
    assert s.unfurl_enabled is False and s.alert_format == "full" and s.webhook_token_hash is None
    await r.set_webhook_token_hash("!r", "abc")
    await r.set_unfurl("!r", True)
    s = await r.get("!r")
    assert s.unfurl_enabled is True and s.webhook_token_hash == "abc"
    assert (await r.find_by_webhook_hash("abc")).room_id == "!r"
    assert await r.find_by_webhook_hash("nope") is None
    await r.set_unfurl("!r", False)
    assert (await r.get("!r")).unfurl_enabled is False


async def test_alert_message(db):
    r = AlertMessageRepo(db)
    assert not await r.seen_dedupe("!r", "k", 60)
    await r.add("$e", "!r", 5, "1234567890123456", 1000, "k")
    assert (await r.get_by_event("$e")).monitor_id == 5
    assert await r.get_by_event("$z") is None
    assert await r.seen_dedupe("!r", "k", 60)
    assert not await r.seen_dedupe("!r2", "k", 60)
    await r.add("$old", "!r", 5, "1", 1, "old", created_at=1)
    assert not await r.seen_dedupe("!r", "old", 60)
