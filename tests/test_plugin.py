"""Security-critical wiring tests for BitsPlugin (constructed without the maubot loader)."""

import base64
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
import yarl
from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer
from mautrix.types import EventType, MessageType
from mautrix.util.async_db import Database

from bitsai.auth.oauth import CompletedLink
from bitsai.auth.pasteback import PASTE_REDIRECT_URI
from bitsai.backend.base import UpstreamError
from bitsai.db import upgrade_table
from bitsai.plugin import MAX_WEBHOOK_BODY, BitsPlugin

BOT = "@bot:x"
ALICE = "@alice:x"
ROOM = "!room:x"
DM = "!dm:x"
PAT = "ddpat_SECRETSECRETSECRET"
KEY = base64.b64encode(os.urandom(32)).decode()

DEFAULTS = {
    "datadog.site": "datadoghq.com", "datadog.subdomain": "",
    "datadog.mcp_toolsets": ["core"], "security.token_encryption_key": KEY,
    "auth.allow_pat_login": True, "auth.oauth_client_name": "t", "auth.oauth_redirect": "auto",
    "chat.command": "bits", "chat.respond_in_dms": True, "chat.respond_to_mentions": True,
    "chat.timeout_seconds": 5, "investigations.poll_max_minutes": 1,
    "rate_limits.per_user_per_minute": 100, "rate_limits.per_room_per_minute": 100,
    "unfurl.default_enabled": False, "admins": [],
}


class Cfg(dict):
    def load_and_update(self):
        pass


class FakeDatadog:
    def __init__(self):
        self.status = 200
        self.seen_auth: list[str] = []
        app = aioweb.Application()
        app.router.add_get("/api/v2/current_user", self.current_user)
        self.server = TestServer(app)

    async def current_user(self, req):
        self.seen_auth.append(req.headers.get("Authorization", ""))
        if self.status != 200:
            return aioweb.json_response({}, status=self.status)
        return aioweb.json_response({"data": {"attributes": {"handle": "al@x.com"}}})


@pytest.fixture
async def dd():
    d = FakeDatadog()
    await d.server.start_server()
    yield d
    await d.server.close()


def members(*users):
    return {u: MagicMock() for u in users}


def power_levels(level):
    pl = MagicMock()
    pl.get_user_level.return_value = level
    return pl


@pytest.fixture
async def plugin(dd, monkeypatch):
    db = Database.create("sqlite::memory:", upgrade_table=upgrade_table,
                         log=logging.getLogger("t"))
    await db.start()
    p = BitsPlugin.__new__(BitsPlugin)
    p.client = AsyncMock()
    p.client.mxid = BOT
    p.client.crypto = None
    p.client.get_joined_members = AsyncMock(return_value=members(BOT, ALICE))
    p.client.get_state_event = AsyncMock(return_value=power_levels(0))
    p.client.create_room = AsyncMock(return_value=DM)
    p.client.send_message_event = AsyncMock(return_value="$sent")
    p.http = aiohttp.ClientSession()
    p.database = db
    p.webapp_url = yarl.URL("https://bot.example/_matrix/maubot/plugin/bits/")
    p.log = logging.getLogger("t")
    # Paste mode: most sign-in tests exercise paste-back; callback tests override it.
    p.config = Cfg({**DEFAULTS, "auth.oauth_redirect": "paste"})
    await p.start()
    # Point PAT validation at the fake Datadog (links only use site.domain).
    p.site = SimpleNamespace(domain="datadoghq.com",
                             api_url=str(dd.server.make_url("")).rstrip("/"))
    yield p
    await p.stop()
    await p.http.close()
    await db.stop()


def make_evt(body, room=ROOM, sender=ALICE, thread=None, edit=None, mentions=None,
             msgtype=MessageType.TEXT):
    evt = MagicMock()
    evt.room_id, evt.sender, evt.event_id = room, sender, "$evt"
    evt.reply = AsyncMock()
    c = evt.content
    c.body, c.msgtype = body, msgtype
    c.get_thread_parent.return_value = thread
    c.get_edit.return_value = edit
    c.serialize.return_value = {"body": body, "m.mentions": {"user_ids": mentions or []}}
    return evt


def run(handler, p, evt, *args):
    return handler.__mb_func__(p, evt, *args)


def all_text(p, evt) -> str:
    """Every string that left the bot: replies plus any sent message content."""
    parts = [str(c) for c in evt.reply.call_args_list]
    parts += [str(c) for c in p.client.send_message_event.call_args_list]
    return "\n".join(parts)


# ---------------------------------------------------------------- 1/2 PAT login

async def test_pat_in_shared_room_redacted_not_saved(plugin, dd):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    evt = make_evt(f"!bits login token {PAT}")
    await run(BitsPlugin.login_cmd, plugin, evt, f"token {PAT}")
    plugin.client.redact.assert_awaited_once()
    assert plugin.client.redact.call_args.args[:2] == (ROOM, "$evt")
    assert await plugin.links.get_link(ALICE) is None
    assert dd.seen_auth == []  # never even sent to Datadog
    assert "revoke" in evt.reply.call_args.args[0].lower()
    plugin.client.create_room.assert_awaited_once()
    assert PAT not in all_text(plugin, evt)


async def test_pat_in_dm_valid_saved(plugin, dd):
    evt = make_evt("x", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, f"token {PAT}")
    plugin.client.redact.assert_awaited_once()
    assert dd.seen_auth == [f"Bearer {PAT}"]
    link = await plugin.links.get_link(ALICE)
    assert link is not None and link.dd_user_handle == "al@x.com"
    assert PAT not in all_text(plugin, evt)


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_pat_in_dm_rejected_not_saved(plugin, dd, status):
    dd.status = status
    evt = make_evt("x", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, f"token {PAT}")
    plugin.client.redact.assert_awaited_once()
    assert await plugin.links.get_link(ALICE) is None
    assert "rejected" in evt.reply.call_args.args[0]


async def test_pat_login_disabled(plugin, dd):
    plugin.config["auth.allow_pat_login"] = False
    evt = make_evt("x", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, f"token {PAT}")
    assert dd.seen_auth == [] and await plugin.links.get_link(ALICE) is None


async def test_pat_redact_failure_still_refuses_shared_room(plugin):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    plugin.client.redact.side_effect = RuntimeError("no perms")
    evt = make_evt("x")
    await run(BitsPlugin.login_cmd, plugin, evt, f"token {PAT}")
    assert await plugin.links.get_link(ALICE) is None


async def test_dm_with_extra_member_not_private(plugin):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    assert not await plugin._is_private_dm(DM, ALICE)
    plugin.client.get_joined_members.side_effect = RuntimeError
    assert not await plugin._is_private_dm(DM, ALICE)


# ---------------------------------------------------------------- 3 OAuth login

async def test_oauth_login_shared_room_no_url(plugin):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    plugin.oauth.begin = AsyncMock(return_value="https://app.datadoghq.com/authorize?state=SECRETSTATE")
    evt = make_evt("!bits login")
    await run(BitsPlugin.login_cmd, plugin, evt, "")
    plugin.oauth.begin.assert_not_called()
    assert "SECRETSTATE" not in all_text(plugin, evt)
    assert "authorize" not in all_text(plugin, evt)
    plugin.client.create_room.assert_awaited_once()


async def test_oauth_login_dm_gets_url(plugin):
    url = "https://app.datadoghq.com/authorize?state=SECRETSTATE"
    plugin.oauth.begin = AsyncMock(return_value=url)
    evt = make_evt("!bits login", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, "")
    plugin.oauth.begin.assert_awaited_once_with(ALICE, dm_room_id=DM)
    assert url in evt.reply.call_args.args[0]
    plugin.client.redact.assert_not_called()


# ---------------------------------------------------------------- 3b OAuth paste-back

PASTED = f"{PASTE_REDIRECT_URI}?code=SECRETCODE&state=SECRETSTATE"


def test_paste_mode_uses_loopback_redirect(plugin):
    assert plugin.oauth_paste
    assert plugin.oauth._redirect_uri == PASTE_REDIRECT_URI


@pytest.mark.parametrize("url,mode,paste", [
    ("http://localhost:29316/_matrix/maubot/plugin/bits/", "auto", False),
    ("https://bot.example/_matrix/maubot/plugin/bits/", "auto", False),
    ("https://bot.example/_matrix/maubot/plugin/bits/", "paste", True),
    ("http://localhost:29316/_matrix/maubot/plugin/bits/", "paste", True),
    ("https://bot.example/_matrix/maubot/plugin/bits/", "callback", False),
])
async def test_redirect_mode_selection(url, mode, paste):
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg({**DEFAULTS, "auth.oauth_redirect": mode})
    p.webapp_url = yarl.URL(url)
    p.log = logging.getLogger("t")
    p.http = p.database = p.client = MagicMock()
    p._spawn = MagicMock(side_effect=lambda coro: coro.close())
    await p.start()
    assert p.oauth_paste is paste
    assert (p.oauth._redirect_uri == PASTE_REDIRECT_URI) is paste
    if not paste:
        assert p.oauth._redirect_uri == url + "oauth/callback"


async def test_bad_redirect_mode_refuses_to_start():
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg({**DEFAULTS, "auth.oauth_redirect": "bogus"})
    p.webapp_url = yarl.URL("https://bot.example/")
    p.log = logging.getLogger("t")
    with pytest.raises(RuntimeError):
        await p.start()


async def test_paste_mode_login_message_has_steps_and_pat_hint(plugin):
    plugin.oauth.begin = AsyncMock(return_value="https://app.datadoghq.com/authorize?x=1")
    evt = make_evt("!bits login", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, "")
    msg = evt.reply.call_args.args[0]
    assert "https://app.datadoghq.com/authorize?x=1" in msg
    assert "paste it here" in msg and "login token" in msg
    plugin.config["auth.allow_pat_login"] = False
    evt = make_evt("!bits login", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, "")
    assert "login token" not in evt.reply.call_args.args[0]


async def test_paste_in_dm_completes_bound_to_sender(plugin, asks):
    plugin.oauth.complete = AsyncMock(return_value=completed())
    evt = make_evt(PASTED, room=DM)
    await plugin.on_message(evt)
    plugin.oauth.complete.assert_awaited_once_with("SECRETSTATE", "SECRETCODE", mxid=ALICE,
                                                   dm_room_id=DM)
    plugin.client.redact.assert_awaited_once()
    assert "Linked as" in evt.reply.call_args.args[0]
    asks.assert_not_called()
    plugin.unfurl.maybe_unfurl.assert_not_called()
    assert "SECRET" not in all_text(plugin, evt)


async def test_paste_via_login_command(plugin):
    plugin.oauth.complete = AsyncMock(return_value=completed())
    evt = make_evt(f"!bits login {PASTED}", room=DM)
    await run(BitsPlugin.login_cmd, plugin, evt, PASTED)
    plugin.oauth.complete.assert_awaited_once()
    plugin.client.redact.assert_awaited_once()


async def test_paste_in_shared_room_redacted_not_used(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    plugin.oauth.complete = AsyncMock()
    evt = make_evt(PASTED)
    await plugin.on_message(evt)
    plugin.client.redact.assert_awaited_once()
    assert plugin.client.redact.call_args.args[:2] == (ROOM, "$evt")
    plugin.oauth.complete.assert_not_called()
    asks.assert_not_called()
    assert "SECRET" not in all_text(plugin, evt)


async def test_paste_denied_cancels(plugin, asks):
    plugin.oauth.cancel = AsyncMock(return_value=True)
    plugin.oauth.complete = AsyncMock()
    evt = make_evt(f"{PASTE_REDIRECT_URI}?error=access_denied&state=SECRETSTATE", room=DM)
    await plugin.on_message(evt)
    plugin.oauth.cancel.assert_awaited_once_with("SECRETSTATE", mxid=ALICE, dm_room_id=DM)
    plugin.oauth.complete.assert_not_called()
    assert "access_denied" in evt.reply.call_args.args[0]


@pytest.mark.parametrize("exc,word", [(ValueError("x"), "expired"),
                                      (UpstreamError("secret detail"), "Couldn't")])
async def test_paste_failures(plugin, asks, exc, word):
    plugin.oauth.complete = AsyncMock(side_effect=exc)
    evt = make_evt(PASTED, room=DM)
    await plugin.on_message(evt)
    assert word in evt.reply.call_args.args[0]
    assert "secret detail" not in all_text(plugin, evt)
    assert await plugin.links.get_link(ALICE) is None


async def test_incomplete_paste_not_forwarded(plugin, asks):
    plugin.oauth.complete = AsyncMock()
    evt = make_evt(f"{PASTE_REDIRECT_URI}?code=SECRETCODE", room=DM)
    await plugin.on_message(evt)
    plugin.oauth.complete.assert_not_called()
    asks.assert_not_called()
    assert "incomplete" in evt.reply.call_args.args[0]


async def test_paste_rejected_in_callback_mode(plugin, asks):
    plugin.oauth_paste = False
    plugin.oauth.complete = AsyncMock()
    evt = make_evt(PASTED, room=DM)
    await plugin.on_message(evt)
    plugin.oauth.complete.assert_not_called()
    plugin.client.redact.assert_awaited_once()


# ---------------------------------------------------------------- 4 login rate limit

async def test_login_rate_limit_fourth_attempt_refused(plugin):
    plugin.oauth.begin = AsyncMock(return_value="https://u/")
    replies = []
    for _ in range(4):
        evt = make_evt("!bits login", room=DM)
        await run(BitsPlugin.login_cmd, plugin, evt, "")
        replies.append(evt.reply.call_args.args[0])
    assert plugin.oauth.begin.await_count == 3
    assert "Too many" in replies[3] and "Too many" not in replies[2]


async def test_login_rate_limit_applies_to_pat(plugin, dd):
    dd.status = 401
    for _ in range(4):
        evt = make_evt("x", room=DM)
        await run(BitsPlugin.login_cmd, plugin, evt, f"token {PAT}")
    assert len(dd.seen_auth) == 3
    assert "Too many" in evt.reply.call_args.args[0]


# ---------------------------------------------------------------- 5 webhook

async def test_webhook_non_admin_refused(plugin):
    evt = make_evt("!bits webhook")
    await run(BitsPlugin.webhook_cmd, plugin, evt, None)
    assert "Only room admins" in evt.reply.call_args.args[0]
    plugin.client.create_room.assert_not_called()
    assert await plugin.room_settings.find_by_webhook_hash("x") is None


async def test_webhook_non_admin_cannot_revoke(plugin):
    plugin.alerts.revoke_webhook = AsyncMock()
    evt = make_evt("!bits webhook revoke")
    await run(BitsPlugin.webhook_cmd, plugin, evt, "revoke")
    plugin.alerts.revoke_webhook.assert_not_called()


async def test_webhook_admin_secret_only_in_dm(plugin):
    plugin.client.get_state_event.return_value = power_levels(50)
    plugin.alerts.create_webhook = AsyncMock(
        return_value=("TOK", "Webhook URL: https://bot/hook/TOK"))
    evt = make_evt("!bits webhook")
    await run(BitsPlugin.webhook_cmd, plugin, evt, None)
    room_reply = evt.reply.call_args.args[0]
    assert "TOK" not in room_reply and "hook/" not in room_reply
    sends = plugin.client.send_message_event.call_args_list
    assert len(sends) == 1
    assert sends[0].args[0] == DM and "hook/TOK" in sends[0].args[2].body


async def test_webhook_real_token_not_in_reply(plugin):
    plugin.client.get_state_event.return_value = power_levels(100)
    evt = make_evt("!bits webhook")
    await run(BitsPlugin.webhook_cmd, plugin, evt, None)
    sent_body = plugin.client.send_message_event.call_args.args[2].body
    token = sent_body.split("/hook/")[1].split("`")[0]
    assert len(token) > 20 and token not in evt.reply.call_args.args[0]


async def test_webhook_dm_failure_revokes(plugin):
    plugin.client.get_state_event.return_value = power_levels(50)
    plugin.client.create_room.side_effect = RuntimeError("boom")
    plugin.alerts.revoke_webhook = AsyncMock()
    evt = make_evt("!bits webhook")
    await run(BitsPlugin.webhook_cmd, plugin, evt, None)
    plugin.alerts.revoke_webhook.assert_awaited_once_with(ROOM)
    plugin.client.send_message_event.assert_not_called()


async def test_webhook_configured_admin_and_revoke(plugin):
    plugin.config["admins"] = [ALICE]
    plugin.alerts.revoke_webhook = AsyncMock()
    evt = make_evt("!bits webhook revoke")
    await run(BitsPlugin.webhook_cmd, plugin, evt, "revoke")
    plugin.alerts.revoke_webhook.assert_awaited_once_with(ROOM)
    assert "revoked" in evt.reply.call_args.args[0]


async def test_webhook_revoke_really_invalidates(plugin):
    plugin.config["admins"] = [ALICE]
    await run(BitsPlugin.webhook_cmd, plugin, make_evt("x"), None)
    token = plugin.client.send_message_event.call_args.args[2].body.split("/hook/")[1].split(
        "`")[0]
    assert await plugin.alerts.handle_webhook(token, b"{}") != 404
    await run(BitsPlugin.webhook_cmd, plugin, make_evt("x"), "revoke")
    assert await plugin.alerts.handle_webhook(token, b"{}") == 404


# ---------------------------------------------------------------- 6 unfurl

async def test_unfurl_non_admin_refused(plugin):
    plugin.unfurl.set_enabled = AsyncMock()
    evt = make_evt("!bits unfurl on")
    await run(BitsPlugin.unfurl_cmd, plugin, evt, "on")
    plugin.unfurl.set_enabled.assert_not_called()
    assert "Only room admins" in evt.reply.call_args.args[0]


async def test_unfurl_admin_ok_and_bad_state(plugin):
    plugin.unfurl.set_enabled = AsyncMock()
    plugin.client.get_state_event.return_value = power_levels(50)
    await run(BitsPlugin.unfurl_cmd, plugin, make_evt("x"), "on")
    plugin.unfurl.set_enabled.assert_awaited_once_with(ROOM, True)
    await run(BitsPlugin.unfurl_cmd, plugin, make_evt("x"), "maybe")
    assert plugin.unfurl.set_enabled.await_count == 1


async def test_context_non_admin_refused(plugin):
    plugin.room_settings.set_thread_context = AsyncMock()
    evt = make_evt("!bits context off")
    await run(BitsPlugin.context_cmd, plugin, evt, "off")
    plugin.room_settings.set_thread_context.assert_not_called()
    assert "Only room admins" in evt.reply.call_args.args[0]


async def test_context_admin_toggles_and_bad_state(plugin):
    plugin.room_settings.set_thread_context = AsyncMock()
    plugin.client.get_state_event.return_value = power_levels(50)
    evt = make_evt("x")
    await run(BitsPlugin.context_cmd, plugin, evt, "off")
    plugin.room_settings.set_thread_context.assert_awaited_once_with(ROOM, False)
    assert "stored in their Bits history" in evt.reply.call_args.args[0]
    await run(BitsPlugin.context_cmd, plugin, make_evt("x"), "maybe")
    assert plugin.room_settings.set_thread_context.await_count == 1


async def test_admin_check_fails_closed(plugin):
    plugin.client.get_state_event.side_effect = RuntimeError
    assert not await plugin._is_room_admin(ROOM, ALICE)
    plugin.client.get_state_event.side_effect = None
    plugin.client.get_state_event.return_value = power_levels(49)
    assert not await plugin._is_room_admin(ROOM, ALICE)


# ---------------------------------------------------------------- 7 OAuth callback

def completed(dm=DM):
    link = SimpleNamespace(mxid=ALICE, dd_user_handle="al@x.com")
    return CompletedLink(link=link, dm_room_id=dm)


def req(**query):
    return SimpleNamespace(query=query)


def assert_hardened(resp):
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("q", [{}, {"state": "s"}, {"code": "c"}, {"state": "", "code": ""}])
async def test_callback_missing_params(plugin, q):
    plugin.oauth.complete = AsyncMock()
    resp = await plugin.oauth_callback(req(**q))
    assert resp.status == 400
    plugin.oauth.complete.assert_not_called()
    assert_hardened(resp)


async def test_callback_bad_state_does_not_echo(plugin):
    resp = await plugin.oauth_callback(req(state="<script>STATEVAL", code="CODEVAL"))
    assert resp.status == 400
    assert "STATEVAL" not in resp.text and "CODEVAL" not in resp.text
    assert "<script>" not in resp.text
    assert_hardened(resp)


async def test_callback_success(plugin):
    plugin.oauth.complete = AsyncMock(return_value=completed())
    plugin.factory.forget = MagicMock()
    resp = await plugin.oauth_callback(req(state="s", code="CODEVAL"))
    assert resp.status == 200
    assert_hardened(resp)
    assert "CODEVAL" not in resp.text
    plugin.factory.forget.assert_called_once_with(ALICE)
    args = plugin.client.send_message_event.call_args.args
    assert args[0] == DM and "al@x.com" in args[2].body and args[2].msgtype is MessageType.NOTICE


async def test_callback_upstream_failure_is_502_and_generic(plugin):
    from bitsai.backend.base import UpstreamError
    plugin.oauth.complete = AsyncMock(side_effect=UpstreamError("secret detail"))
    resp = await plugin.oauth_callback(req(state="s", code="c"))
    assert resp.status == 502 and "secret detail" not in resp.text
    assert_hardened(resp)


async def test_callback_notice_failure_still_200(plugin):
    plugin.oauth.complete = AsyncMock(return_value=completed())
    plugin.client.send_message_event.side_effect = RuntimeError
    resp = await plugin.oauth_callback(req(state="s", code="c"))
    assert resp.status == 200


# ---------------------------------------------------------------- 8 webhook route

class FakeContent:
    """Stream stub: consumes data, optionally returning at most ``chunk`` bytes per read."""

    def __init__(self, data, chunk=None):
        self.data = data
        self.chunk = chunk

    async def read(self, n):
        n = min(n, self.chunk) if self.chunk else n
        out, self.data = self.data[:n], self.data[n:]
        return out


async def test_webhook_route_reassembles_partial_reads(plugin):
    plugin.alerts.handle_webhook = AsyncMock(return_value=202)
    body = b'{"title": "' + b"y" * 5000 + b'"}'
    r = SimpleNamespace(content=FakeContent(body, chunk=100), match_info={"token": "TOK"})
    await plugin.webhook(r)
    plugin.alerts.handle_webhook.assert_awaited_once_with("TOK", body)


async def test_webhook_route_passes_body_and_status(plugin):
    plugin.alerts.handle_webhook = AsyncMock(return_value=202)
    r = SimpleNamespace(content=FakeContent(b'{"a":1}'), match_info={"token": "TOK"})
    resp = await plugin.webhook(r)
    plugin.alerts.handle_webhook.assert_awaited_once_with("TOK", b'{"a":1}')
    assert resp.status == 202 and resp.headers["Cache-Control"] == "no-store"


async def test_webhook_route_truncates_to_limit_plus_one(plugin):
    plugin.alerts.handle_webhook = AsyncMock(return_value=400)
    r = SimpleNamespace(content=FakeContent(b"x" * (MAX_WEBHOOK_BODY * 3)),
                        match_info={"token": "TOK"})
    resp = await plugin.webhook(r)
    assert len(plugin.alerts.handle_webhook.call_args.args[1]) == MAX_WEBHOOK_BODY + 1
    assert resp.status == 400


async def test_webhook_route_oversize_over_real_http_rejected(plugin):
    """StreamReader.read(n) may return a partial chunk; oversize bodies must still be 400."""
    plugin.config["admins"] = [ALICE]
    await run(BitsPlugin.webhook_cmd, plugin, make_evt("x"), None)
    token = plugin.client.send_message_event.call_args.args[2].body.split("/hook/")[1].split(
        "`")[0]
    app = aioweb.Application()
    app.router.add_post("/hook/{token}", lambda r: plugin.webhook(r))
    async with TestClient(TestServer(app)) as c:
        big = b'{"pad":"' + b"x" * (MAX_WEBHOOK_BODY * 4) + b'"}'
        resp = await c.post(f"/hook/{token}", data=big)
        assert resp.status == 400


# ---------------------------------------------------------------- 9 on_message

@pytest.fixture
def asks(plugin):
    plugin.chat.ask = AsyncMock()
    plugin.unfurl.maybe_unfurl = AsyncMock()
    return plugin.chat.ask


async def test_own_message_ignored(plugin, asks):
    await plugin.on_message(make_evt("hi", sender=BOT, room=DM))
    asks.assert_not_called()
    plugin.unfurl.maybe_unfurl.assert_not_called()


async def test_bang_prefix_ignored(plugin, asks):
    await plugin.on_message(make_evt("!bits ask hi", room=DM))
    asks.assert_not_called()


async def test_edit_and_non_text_ignored(plugin, asks):
    await plugin.on_message(make_evt("hi", room=DM, edit="$old"))
    await plugin.on_message(make_evt("hi", room=DM, msgtype=MessageType.NOTICE))
    asks.assert_not_called()


async def test_dm_message_triggers_ask(plugin, asks):
    await plugin.on_message(make_evt("what is up", room=DM))
    asks.assert_awaited_once_with(DM, "$evt", ALICE, "what is up")


async def test_shared_room_without_mention_no_ask(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    await plugin.on_message(make_evt("hello all"))
    asks.assert_not_called()


async def test_shared_room_with_mention_asks(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    await plugin.on_message(make_evt("hey bot", mentions=[BOT]))
    asks.assert_awaited_once()


async def test_mention_of_someone_else_no_ask(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    await plugin.on_message(make_evt("hey eve", mentions=["@eve:x"]))
    asks.assert_not_called()


async def test_thread_followup_only_for_own_conversation(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")

    async def get(room, root, owner):
        return SimpleNamespace(owner_mxid=ALICE) if owner == ALICE else None

    plugin.conversations.get = AsyncMock(side_effect=get)
    await plugin.on_message(make_evt("more?", thread="$root"))
    asks.assert_awaited_once_with(ROOM, "$root", ALICE, "more?")
    asks.reset_mock()
    await plugin.on_message(make_evt("no mention", thread="$root", sender="@eve:x"))
    asks.assert_not_called()


async def test_thread_mention_by_non_owner_asks(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    plugin.conversations.get = AsyncMock(return_value=None)
    await plugin.on_message(
        make_evt("hey bot, join", thread="$root", sender="@eve:x", mentions=[BOT])
    )
    asks.assert_awaited_once_with(ROOM, "$root", "@eve:x", "hey bot, join")


# ---------------------------------------------------------------- 10 on_reaction

def reaction(sender=ALICE):
    e = MagicMock()
    e.sender, e.room_id = sender, ROOM
    e.content.relates_to.event_id, e.content.relates_to.key = "$target", "🔍"
    return e


async def test_own_reaction_ignored(plugin):
    plugin.chat.on_reaction = AsyncMock()
    plugin.alerts.on_reaction = AsyncMock()
    await plugin.on_reaction(reaction(BOT))
    plugin.chat.on_reaction.assert_not_called()
    plugin.alerts.on_reaction.assert_not_called()


async def test_reaction_chat_first_then_alerts(plugin):
    order = []
    plugin.chat.on_reaction = AsyncMock(side_effect=lambda *a: order.append("chat") or True)
    plugin.alerts.on_reaction = AsyncMock(side_effect=lambda *a, **k: order.append("alerts"))
    await plugin.on_reaction(reaction())
    assert order == ["chat"]
    plugin.chat.on_reaction = AsyncMock(side_effect=lambda *a: order.append("chat") or False)
    order.clear()
    await plugin.on_reaction(reaction())
    assert order == ["chat", "alerts"]
    assert plugin.alerts.on_reaction.call_args.kwargs["bot_mxid"] == BOT


# ---------------------------------------------------------------- 11 start()

@pytest.mark.parametrize("bad", ["", "not-base64-at-all!!", base64.b64encode(b"short").decode()])
async def test_invalid_key_raises_without_leaking(bad):
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg({**DEFAULTS, "security.token_encryption_key": bad})
    p.webapp_url = yarl.URL("https://bot.example/")
    p.log = logging.getLogger("t")
    with pytest.raises(RuntimeError) as ei:
        await p.start()
    if bad:
        assert bad not in str(ei.value)
    assert ei.value.__cause__ is None


async def test_start_requires_webapp():
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg(DEFAULTS)
    p.webapp_url = None
    with pytest.raises(RuntimeError):
        await p.start()


async def test_failed_start_unregisters_handlers():
    # maubot registers handlers before start() and never removes them if it raises; we must.
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg(DEFAULTS)
    p.webapp_url = None
    p.webapp = MagicMock()
    p.client = MagicMock()
    handler = object()
    p._handlers_at_startup = [(handler, EventType.ROOM_MESSAGE)]
    with pytest.raises(RuntimeError):
        await p.start()
    p.client.remove_event_handler.assert_called_once_with(EventType.ROOM_MESSAGE, handler)
    p.webapp.clear.assert_called_once()
    assert p._handlers_at_startup == []
    assert p.cmd == "bits"  # stale command matchers must not raise AttributeError


async def test_open_dm_reuses_existing_private_dm(plugin):
    rooms = {ROOM: members(BOT, ALICE, "@eve:x"), DM: members(BOT, ALICE)}
    plugin.client.get_joined_rooms = AsyncMock(return_value=[ROOM, DM])
    plugin.client.get_joined_members = AsyncMock(side_effect=lambda r: rooms[r])
    plugin.client.create_room = AsyncMock(return_value="!new:x")
    assert await plugin._open_dm(ALICE) == (DM, False)
    plugin.client.create_room.assert_not_awaited()


async def test_open_dm_creates_when_none_exists(plugin):
    plugin.client.get_joined_rooms = AsyncMock(return_value=[ROOM])
    plugin.client.get_joined_members = AsyncMock(return_value=members(BOT, ALICE, "@eve:x"))
    plugin.client.create_room = AsyncMock(return_value="!new:x")
    assert await plugin._open_dm(ALICE) == ("!new:x", True)


async def test_plain_text_mention_asks_with_mention_stripped(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    await plugin.on_message(make_evt("bot: why is checkout slow?"))
    asks.assert_awaited_once()
    assert asks.await_args.args[-1] == "why is checkout slow?"


async def test_bare_mention_gets_hint_not_ask(plugin, asks):
    plugin.client.get_joined_members.return_value = members(BOT, ALICE, "@eve:x")
    evt = make_evt("bot:")
    await plugin.on_message(evt)
    asks.assert_not_awaited()
    evt.reply.assert_awaited_once()


async def test_callback_success_names_the_matrix_account_escaped(plugin):
    link = SimpleNamespace(mxid="@<b>mallory</b>:x", dd_user_handle="al@x.com")
    plugin.oauth.complete = AsyncMock(return_value=CompletedLink(link=link, dm_room_id=DM))
    plugin.factory.forget = MagicMock()
    resp = await plugin.oauth_callback(req(state="s", code="c"))
    assert resp.status == 200
    assert "@&lt;b&gt;mallory&lt;/b&gt;:x" in resp.text
    assert "<b>mallory</b>" not in resp.text


async def test_public_callback_mode_logs_url_to_allowlist(caplog):
    url = "https://bot.example/_matrix/maubot/plugin/bits/"
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg({**DEFAULTS, "auth.oauth_redirect": "callback"})
    p.webapp_url = yarl.URL(url)
    p.log = logging.getLogger("t")
    p.http = p.database = p.client = MagicMock()
    p._spawn = MagicMock(side_effect=lambda coro: coro.close())
    with caplog.at_level(logging.INFO, logger="t"):
        await p.start()
    assert any(url + "oauth/callback" in r.getMessage() and "MCP OAuth Redirect URLs"
               in r.getMessage() for r in caplog.records)


async def _started(url, **cfg):
    p = BitsPlugin.__new__(BitsPlugin)
    p.config = Cfg({**DEFAULTS, **cfg})
    p.webapp_url = yarl.URL(url)
    p.log = logging.getLogger("t")
    p.http = p.database = p.client = MagicMock()
    p._spawn = MagicMock(side_effect=lambda coro: coro.close())
    await p.start()
    return p


async def test_config_save_applies_without_manual_restart():
    url = "https://bot.example/_matrix/maubot/plugin/bits/"
    p = await _started(url, **{"auth.oauth_redirect": "paste"})
    assert p.oauth_paste is True
    p.config["auth.oauth_redirect"] = "auto"
    await p.on_external_config_update()
    assert p.oauth_paste is False
    assert p.oauth._redirect_uri == url + "oauth/callback"


async def test_invalid_config_save_keeps_running_settings(caplog):
    p = await _started("https://bot.example/_matrix/maubot/plugin/bits/",
                       **{"auth.oauth_redirect": "paste"})
    oauth = p.oauth
    p.config["auth.oauth_redirect"] = "bogus"
    with caplog.at_level(logging.ERROR, logger="t"):
        await p.on_external_config_update()
    assert p.oauth is oauth and p.oauth_paste is True
    assert any("not applied" in r.getMessage() for r in caplog.records)


async def test_startup_logs_redirect_uri(caplog):
    with caplog.at_level(logging.INFO, logger="t"):
        await _started("http://localhost:29316/_matrix/maubot/plugin/bits/",
                       **{"auth.oauth_redirect": "callback"})
    assert any("redirect_uri=http://localhost:29316/_matrix/maubot/plugin/bits/oauth/callback"
               in r.getMessage() for r in caplog.records)


async def test_paste_mode_log_names_setting_and_callback_url(caplog):
    url = "https://cog.example/_matrix/maubot/plugin/bits/"
    with caplog.at_level(logging.INFO, logger="t"):
        await _started(url, **{"auth.oauth_redirect": "paste"})
    msg = next(r.getMessage() for r in caplog.records if "OAuth sign-in" in r.getMessage())
    assert "paste mode (auth.oauth_redirect=paste)" in msg
    assert url + "oauth/callback" in msg and "public_url" not in msg
