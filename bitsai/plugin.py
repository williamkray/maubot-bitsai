"""Bits AI for Matrix: plugin wiring, commands, event routing and web routes.

Security-relevant rules implemented here (see PLAN.md "Security requirements"):
- Sign-in links and PATs are only accepted in a verified 1:1 DM (exactly 2 joined members).
- A message containing a PAT is always redacted, wherever it was sent.
- Room-level settings (webhooks, unfurls) require power level >= 50 or a configured admin.
- Web routes are unauthenticated: OAuth callbacks are bound by single-use state; webhooks by a
  per-room secret whose hash is stored.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any

import aiohttp
from aiohttp import web as aioweb
from maubot import MessageEvent, Plugin
from maubot.handlers import command, event, web
from mautrix.types import (
    EventID,
    EventType,
    MessageType,
    PowerLevelStateEventContent,
    ReactionEvent,
    RoomCreatePreset,
    RoomID,
    TextMessageEventContent,
    UserID,
)
from mautrix.util.async_db import UpgradeTable
from mautrix.util.config import BaseProxyConfig

from .auth.base import CredentialKind
from .auth.crypto import SecretBox
from .auth.links import LinkRepository, TokenManager
from .auth.oauth import DatadogOAuth
from .auth.pasteback import PASTE_REDIRECT_URI, contains_redirect, parse_redirect
from .backend.base import AuthRequired, BackendError, BitsBackend, UpstreamError
from .backend.mcp import McpBackend
from .backend.rest import RestBackend
from .config import Config
from .db import upgrade_table
from .matrix.mentions import extract_question
from .matrix.messenger import MatrixMessenger
from .matrix.render import render_notice
from .mcp.client import McpClient
from .ratelimit import RateLimiter
from .repos import AlertMessageRepo, ConversationRepo, InvestigationRepo, RoomSettingsRepo
from .services.alerts import AlertService
from .services.chat import ChatService
from .services.investigations import InvestigationService
from .services.unfurl import UnfurlService
from .sites import Site

ADMIN_POWER_LEVEL = 50
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_WEBHOOK_BODY = 64 * 1024
_NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Type": "text/html; charset=utf-8",
}
_PAGE = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Bits AI for Matrix</title><body style='font-family:sans-serif;margin:3em'>"
    "<h2>{title}</h2><p>{text}</p></body>"
)


def _notice(text: str) -> TextMessageEventContent:
    return render_notice(text)


class _Factory:
    """BackendFactory: one cached MCP session per user, always acting as that user."""

    def __init__(self, plugin: BitsPlugin) -> None:
        self._p = plugin
        self._cache: dict[UserID, BitsBackend] = {}

    def forget(self, mxid: UserID) -> None:
        self._cache.pop(mxid, None)

    async def for_user(self, mxid: UserID) -> BitsBackend:
        await self._p.tokens.get(mxid)  # raises AuthRequired if not linked / unrefreshable
        backend = self._cache.get(mxid)
        if backend is None:

            async def token() -> str:
                return (await self._p.tokens.get(mxid)).access_token

            p = self._p
            rest = RestBackend(p.http, p.site, token)
            session = McpClient(p.http, p.site.mcp_url, token, p.config["datadog.mcp_toolsets"],
                                p.log.getChild("mcp"))
            backend = McpBackend(session, p.site, rest)
            self._cache[mxid] = backend
        return backend


class BitsPlugin(Plugin):
    """Bits AI for Matrix."""

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable | None:
        return upgrade_table

    # ------------------------------------------------------------------ lifecycle

    cmd: str = "bits"  # real value set in start(); default keeps stale handlers harmless

    async def start(self) -> None:
        # maubot registers our handlers *before* start() and does not remove them if start()
        # raises, so a failed start would leave commands firing on a half-built plugin.
        try:
            await self._start()
        except BaseException:
            handlers = getattr(self, "_handlers_at_startup", [])
            for func, event_type in handlers:
                self.client.remove_event_handler(event_type, func)
            handlers.clear()
            if getattr(self, "webapp", None) is not None:
                self.webapp.clear()
            for t in list(getattr(self, "_tasks", ())):
                t.cancel()
            raise

    async def on_external_config_update(self) -> None:
        """Apply a config saved in the maubot UI without a manual restart (most settings are
        only read in ``_start``). An invalid config is not applied: we keep running as before."""
        self.config.load_and_update()
        problem = self._config_problem()
        if problem:
            self.log.error("New config not applied (%s); still running with the previous "
                           "settings", problem)
            return
        self.log.info("Config changed; restarting to apply it")
        await self.stop()
        await self.start()

    def _config_problem(self) -> str | None:
        """Why ``_start`` would refuse this config, or None."""
        if self.webapp_url is None:
            return "bitsai requires `webapp: true` and a maubot public_url"
        try:
            SecretBox(self.config["security.token_encryption_key"] or "")
        except ValueError as e:
            return f"invalid security.token_encryption_key: {e}"
        if self._redirect_mode() not in ("callback", "paste"):
            return "auth.oauth_redirect must be auto, paste or callback"
        return None

    def _redirect_mode(self) -> str:
        # auto = redirect straight to this maubot's public_url (localhost or not). Datadog only
        # needs the URL allow-listed when it isn't loopback; paste is the opt-in fallback.
        mode = (self.config["auth.oauth_redirect"] or "auto").strip().lower()
        return "callback" if mode == "auto" else mode

    async def _start(self) -> None:
        self.config.load_and_update()
        problem = self._config_problem()
        if problem:
            raise RuntimeError(f"bitsai: {problem}")
        box = SecretBox(self.config["security.token_encryption_key"])
        self.site = Site(self.config["datadog.site"],
                         (self.config["datadog.subdomain"] or "").strip().lower())
        loopback = self.webapp_url.host in _LOOPBACK_HOSTS
        self.oauth_paste = self._redirect_mode() == "paste"
        callback_url = str(self.webapp_url / "oauth" / "callback")
        redirect_uri = PASTE_REDIRECT_URI if self.oauth_paste else callback_url
        setting = (self.config["auth.oauth_redirect"] or "auto").strip().lower()
        if self.oauth_paste:
            self.log.info("OAuth sign-in: paste mode (auth.oauth_redirect=%s), redirect_uri=%s "
                          "(fixed loopback). To have Datadog redirect here instead, set "
                          "auth.oauth_redirect: callback and allow-list %s in Datadog",
                          setting, redirect_uri, callback_url)
        else:
            self.log.info("OAuth sign-in: callback mode (auth.oauth_redirect=%s), redirect_uri=%s "
                          "(from maubot server.public_url)", setting, redirect_uri)
        if not self.oauth_paste and not loopback:
            # Datadog accepts loopback redirects by default; anything else must be allow-listed
            # per org, or /authorize fails with "Mismatching redirect URI" (docs/FINDINGS.md §5).
            self.log.info("auth.oauth_redirect=callback: a Datadog org admin must add %s to "
                          "Organization Settings > MCP Server > MCP OAuth Redirect URLs",
                          callback_url)
        if self.webapp_url.scheme != "https" and not loopback:
            self.log.warning("maubot public_url is not https; webhooks will be insecure")

        self.links = LinkRepository(self.database, box)
        client_name = self.config["auth.oauth_client_name"] or (
            f"maubot-bitsai ({self.webapp_url.host})"
        )
        self.oauth = DatadogOAuth(
            self.http, self.database, box, self.site,
            redirect_uri=redirect_uri,
            client_name=client_name, log=self.log.getChild("oauth"),
        )
        self.tokens = TokenManager(self.links, self.oauth)
        self.factory = _Factory(self)
        self.cmd = self.config["chat.command"]
        self.messenger = MatrixMessenger(self.client, self.cmd, self._open_dm,
                                         self._is_private_dm)

        self.conversations = ConversationRepo(self.database)
        self.room_settings = RoomSettingsRepo(self.database)
        self.chat = ChatService(
            self.factory, self.messenger, self.conversations, self.site,
            self.log.getChild("chat"), timeout_s=self.config["chat.timeout_seconds"],
            settings=self.room_settings,
        )
        self.investigations = InvestigationService(
            self.factory, self.messenger, InvestigationRepo(self.database), self.site,
            self.log.getChild("investigations"),
            max_minutes=self.config["investigations.poll_max_minutes"],
        )
        self.alerts = AlertService(
            self.messenger, self.room_settings, AlertMessageRepo(self.database), self.site,
            self.log.getChild("alerts"),
            on_investigate=self._investigate_alert, on_ask=self._ask_guarded,
        )
        self.unfurl = UnfurlService(
            self.factory, self.messenger, self.room_settings, self.site,
            self.log.getChild("unfurl"), default_enabled=self.config["unfurl.default_enabled"],
        )
        self.user_limit = RateLimiter(self.config["rate_limits.per_user_per_minute"], 60)
        self.room_limit = RateLimiter(self.config["rate_limits.per_room_per_minute"], 60)
        self.login_limit = RateLimiter(3, 600)
        self._tasks: set[asyncio.Task] = set()
        self._dm_rooms: dict[UserID, RoomID] = {}
        self._displayname: str | None = None
        self._spawn(self.investigations.resume_open())
        self._spawn(self._load_displayname())

    async def stop(self) -> None:
        # Cancel our own tasks first: a still-running resume_open() could otherwise spawn
        # poll tasks after the investigation service has collected its tasks to cancel.
        tasks = list(getattr(self, "_tasks", ()))
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for svc in ("chat", "investigations", "alerts"):
            if hasattr(self, svc):
                await getattr(self, svc).stop()

    async def _load_displayname(self) -> None:
        try:
            name = await self.client.get_displayname(self.client.mxid)
            self._displayname = name if isinstance(name, str) and name.strip() else None
        except Exception:
            self.log.debug("Could not fetch own display name")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ helpers

    async def _is_private_dm(self, room_id: RoomID, user_id: UserID) -> bool:
        try:
            members = await self.client.get_joined_members(room_id)
        except Exception:
            return False
        return set(members) == {self.client.mxid, user_id}

    async def _dm_usable(self, room_id: RoomID, user_id: UserID) -> bool:
        """True if we're joined and nobody but ``user_id`` is (they may still be invited)."""
        try:
            members = set(await self.client.get_joined_members(room_id))
        except Exception:
            return False
        return self.client.mxid in members and members <= {self.client.mxid, user_id}

    async def _find_dm(self, user_id: UserID) -> RoomID | None:
        cached = self._dm_rooms.get(user_id)
        if cached and await self._dm_usable(cached, user_id):
            return cached
        self._dm_rooms.pop(user_id, None)
        try:
            rooms = await self.client.get_joined_rooms()
        except Exception:
            return None
        for room_id in rooms[:500]:
            if await self._is_private_dm(room_id, user_id):
                self._dm_rooms[user_id] = room_id
                return room_id
        return None

    async def _open_dm(self, user_id: UserID) -> tuple[RoomID | None, bool]:
        """Return ``(room, created)``: an existing 1:1 DM with ``user_id`` if there is one,
        else a newly created DM the user has been invited to (``created=True``)."""
        existing = await self._find_dm(user_id)
        if existing:
            return existing, False
        initial_state = []
        if getattr(self.client, "crypto", None):
            initial_state.append({"type": "m.room.encryption", "state_key": "",
                                  "content": {"algorithm": "m.megolm.v1.aes-sha2"}})
        try:
            room = await self.client.create_room(
                preset=RoomCreatePreset.TRUSTED_PRIVATE, is_direct=True,
                invitees=[user_id], name="Bits AI", initial_state=initial_state,
            )
        except Exception:
            self.log.exception("Could not open DM with %s", user_id)
            return None, False
        self._dm_rooms[user_id] = room
        return room, True

    async def _is_room_admin(self, room_id: RoomID, user_id: UserID) -> bool:
        if user_id in (self.config["admins"] or []):
            return True
        try:
            pl: PowerLevelStateEventContent = await self.client.get_state_event(
                room_id, EventType.ROOM_POWER_LEVELS
            )
        except Exception:
            return False
        return pl.get_user_level(user_id) >= ADMIN_POWER_LEVEL

    @staticmethod
    def _thread_root(evt: MessageEvent) -> EventID:
        return evt.content.get_thread_parent() or evt.event_id

    def _allowed(self, evt: MessageEvent) -> bool:
        return self.user_limit.allow(evt.sender) and self.room_limit.allow(evt.room_id)

    async def _ask_guarded(self, room_id: RoomID, thread_root: EventID, sender: UserID,
                           text: str) -> None:
        if not (self.user_limit.allow(sender) and self.room_limit.allow(room_id)):
            await self.messenger.send(room_id, _notice("Slow down a little — rate limit hit."),
                                      thread_root=thread_root)
            return
        await self.chat.ask(room_id, thread_root, sender, text)

    async def _investigate_alert(self, room_id, thread_root, sender, alert) -> None:
        if not (self.user_limit.allow(sender) and self.room_limit.allow(room_id)):
            return
        await self.investigations.start_from_alert(room_id, thread_root, sender, alert)

    # ------------------------------------------------------------------ commands

    @command.new(name=lambda self: self.cmd, help="Bits AI for Matrix", require_subcommand=True)
    async def bits(self, evt: MessageEvent) -> None:
        pass

    @bits.subcommand("help", help="Show usage")
    async def help_cmd(self, evt: MessageEvent) -> None:
        c = self.cmd
        await evt.reply(
            f"**Bits AI for Matrix**\n\n"
            f"- `!{c} ask <question>` — ask Bits (replies in a thread; reply in the thread to "
            f"follow up)\n"
            f"- `!{c} investigate <description>` — start a Bits AI SRE investigation\n"
            f"- `!{c} steer <message>` — (in an investigation thread) steer or resume it\n"
            f"- `!{c} login` — link your Datadog account (DM only)\n"
            f"- `!{c} login token <PAT>` — link with a Personal Access Token (DM only)\n"
            f"- `!{c} logout` / `!{c} whoami`\n"
            f"- `!{c} webhook` / `!{c} webhook revoke` — room admins: Datadog alert webhook\n"
            f"- `!{c} unfurl on|off` — room admins: Datadog link previews\n"
            f"- `!{c} context on|off` — room admins: seed new thread participants' Bits "
            f"conversations with the thread's messages\n\n"
            f"In a shared thread, everyone gets their own Bits conversation using their own "
            f"Datadog access.\n\n"
            f"React 🔍 on an alert to investigate it, 💬 to ask Bits about it."
        )

    @bits.subcommand("ask", help="Ask Bits AI a question")
    @command.argument("question", pass_raw=True)
    async def ask_cmd(self, evt: MessageEvent, question: str) -> None:
        if not question.strip():
            await evt.reply(f"Usage: `!{self.cmd} ask <question>`")
            return
        await self._ask_guarded(evt.room_id, self._thread_root(evt), evt.sender, question)

    @bits.subcommand("investigate", help="Start a Bits AI SRE investigation")
    @command.argument("description", pass_raw=True)
    async def investigate_cmd(self, evt: MessageEvent, description: str) -> None:
        if not self._allowed(evt):
            await evt.reply("Slow down a little — rate limit hit.")
            return
        await self.investigations.start_general(
            evt.room_id, self._thread_root(evt), evt.sender, description
        )

    @bits.subcommand("steer", help="Steer the investigation in this thread")
    @command.argument("message", pass_raw=True)
    async def steer_cmd(self, evt: MessageEvent, message: str) -> None:
        root = evt.content.get_thread_parent()
        if not root:
            await evt.reply("Use this inside an investigation's thread.")
            return
        await self.investigations.steer(evt.room_id, root, evt.sender, message)

    @bits.subcommand("login", help="Link your Datadog account (DM only)")
    @command.argument("args", pass_raw=True, required=False)
    async def login_cmd(self, evt: MessageEvent, args: str = "") -> None:
        if contains_redirect(args or ""):
            await self._login_paste(evt, args)
            return
        parts = (args or "").split()
        is_pat = bool(parts) and parts[0].lower() == "token"
        if is_pat:
            # Always remove the secret from the room first, wherever it was sent.
            try:
                await self.client.redact(evt.room_id, evt.event_id, reason="credential")
            except Exception:
                self.log.warning("Could not redact a message containing a credential")
        if not await self._is_private_dm(evt.room_id, evt.sender):
            dm, created = await self._open_dm(evt.sender)
            warn = (" Your token was posted in a shared room: **revoke it in Datadog** and "
                    "create a new one." if is_pat else "")
            if dm is None:
                where = "Send me this command in a direct chat."
            elif created:
                where = "I've invited you to a direct chat; run the command there."
            else:
                where = "Run the command in our direct chat."
                await self.messenger.send(dm, _notice(
                    f"You asked to sign in from another room. Run `!{self.cmd} login` here."))
            await evt.reply(where + warn)
            return
        if not self.login_limit.allow(evt.sender):
            await evt.reply("Too many sign-in attempts; try again in a few minutes.")
            return
        if is_pat:
            await self._login_pat(evt, parts[1] if len(parts) > 1 else "")
            return
        try:
            url = await self.oauth.begin(evt.sender, dm_room_id=evt.room_id)
        except UpstreamError as e:
            await evt.reply(f"Couldn't start sign-in: {e}")
            return
        pat_hint = (f"\n\nOr link with a personal access token: "
                    f"`!{self.cmd} login token <PAT>`." if self.config["auth.allow_pat_login"]
                    else "")
        if not self.oauth_paste:
            await evt.reply(
                f"[Sign in to Datadog]({url}) — this link is for you only, works once, and "
                f"expires in 10 minutes. Don't share it.{pat_hint}"
            )
            return
        await evt.reply(
            f"**Sign in to Datadog** — this link is for you only, works once, and expires in "
            f"10 minutes. Don't share it.\n\n"
            f"1. Open [this sign-in link]({url}) and approve access.\n"
            f"2. Your browser then fails to open a `localhost` page. That's expected.\n"
            f"3. Copy the whole address from the address bar and paste it here.{pat_hint}"
        )

    async def _login_paste(self, evt: MessageEvent, text: str) -> None:
        """Finish OAuth from a pasted loopback redirect URL (see auth/pasteback.py)."""
        # The URL carries a live authorization code: always remove it from the room.
        try:
            await self.client.redact(evt.room_id, evt.event_id, reason="credential")
        except Exception:
            self.log.warning("Could not redact a message containing a credential")
        if not await self._is_private_dm(evt.room_id, evt.sender):
            await evt.reply("Paste that address in our direct chat, not here (I removed it).")
            return
        if not self.oauth_paste:
            await evt.reply(f"This bot doesn't use pasted sign-in. Run `!{self.cmd} login`.")
            return
        pasted = parse_redirect(text)
        if pasted is None:
            await evt.reply("That address looks incomplete. Copy the whole address from the "
                            "browser's address bar (it contains `code=` and `state=`).")
            return
        if pasted.error:
            await self.oauth.cancel(pasted.state, mxid=evt.sender, dm_room_id=evt.room_id)
            await evt.reply(f"Datadog didn't authorize the sign-in (`{pasted.error}`). "
                            f"Run `!{self.cmd} login` to try again.")
            return
        try:
            done = await self.oauth.complete(pasted.state, pasted.code, mxid=evt.sender,
                                             dm_room_id=evt.room_id)
        except ValueError:
            await evt.reply(f"That sign-in link is invalid, expired, or already used. "
                            f"Run `!{self.cmd} login` again.")
            return
        except (UpstreamError, AuthRequired):
            await evt.reply("Couldn't complete sign-in with Datadog. Try again shortly.")
            return
        self.factory.forget(done.link.mxid)
        who = done.link.dd_user_handle or "identity unknown"
        await evt.reply(f"Linked as **{who}**. Not you? `!{self.cmd} logout`")

    async def _login_pat(self, evt: MessageEvent, token: str) -> None:
        if not self.config["auth.allow_pat_login"]:
            await evt.reply("Token sign-in is disabled on this bot; use `login` instead.")
            return
        if not token or len(token) > 512:
            await evt.reply(f"Usage: `!{self.cmd} login token <personal access token>`")
            return
        handle = await self._validate_pat(token)
        if handle is False:
            await evt.reply("Datadog rejected that token (or couldn't be reached).")
            return
        link = await self.links.save_pat(evt.sender, self.site.domain, token, handle or None)
        self.factory.forget(evt.sender)
        await evt.reply(f"Linked as **{link.dd_user_handle or 'unknown user'}** (token). "
                        f"Not you? `!{self.cmd} logout`")

    async def _validate_pat(self, token: str) -> str | bool:
        """Return the handle ('' if unknown) when Datadog accepts the token, else False."""
        try:
            async with self.http.get(
                f"{self.site.api_url}/api/v2/current_user",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return False
        attrs = ((data or {}).get("data") or {}).get("attributes") or {}
        handle = attrs.get("handle") or attrs.get("email") or ""
        return "".join(c for c in str(handle)[:200] if c.isprintable())

    @bits.subcommand("logout", help="Unlink your Datadog account")
    async def logout_cmd(self, evt: MessageEvent) -> None:
        removed = await self.links.delete(evt.sender)
        self.factory.forget(evt.sender)
        await evt.reply(
            ("Unlinked. " if removed else "You weren't linked. ")
            + self.oauth.revoke_instructions(self.site)
        )

    @bits.subcommand("whoami", help="Show your linked Datadog account")
    async def whoami_cmd(self, evt: MessageEvent) -> None:
        link = await self.links.get_link(evt.sender)
        if not link:
            await evt.reply(f"Not linked. DM me `!{self.cmd} login`.")
            return
        kind = "OAuth" if link.kind is CredentialKind.OAUTH else "personal access token"
        await evt.reply(f"Linked to **{link.dd_user_handle or 'unknown user'}** on "
                        f"{link.site} via {kind}.")

    @bits.subcommand("webhook", help="Room admins: set up or revoke the Datadog alert webhook")
    @command.argument("action", required=False)
    async def webhook_cmd(self, evt: MessageEvent, action: str | None = None) -> None:
        if not await self._is_room_admin(evt.room_id, evt.sender):
            await evt.reply("Only room admins can manage the webhook.")
            return
        if action == "revoke":
            await self.alerts.revoke_webhook(evt.room_id)
            await evt.reply("Webhook revoked; Datadog posts to the old URL will be rejected.")
            return
        _token, instructions = await self.alerts.create_webhook(evt.room_id, str(self.webapp_url))
        # The URL is a secret: deliver it privately to the admin, never in the room.
        dm, _created = await self._open_dm(evt.sender)
        if dm is None:
            await self.alerts.revoke_webhook(evt.room_id)
            await evt.reply("Couldn't open a direct chat to send you the webhook URL.")
            return
        await self.messenger.send(
            dm, render_notice(f"Webhook for {evt.room_id}:\n\n{instructions}")
        )
        await evt.reply("Webhook created (any previous URL is now invalid). I've sent the "
                        "setup details to you in a direct chat.")

    @bits.subcommand("unfurl", help="Room admins: turn Datadog link previews on or off")
    @command.argument("state")
    async def unfurl_cmd(self, evt: MessageEvent, state: str) -> None:
        if state not in ("on", "off"):
            await evt.reply(f"Usage: `!{self.cmd} unfurl on|off`")
            return
        if not await self._is_room_admin(evt.room_id, evt.sender):
            await evt.reply("Only room admins can change link previews.")
            return
        await self.unfurl.set_enabled(evt.room_id, state == "on")
        await evt.reply(
            "Link previews on. Previews use the poster's Datadog access and are visible to "
            "everyone in this room." if state == "on" else "Link previews off."
        )

    @bits.subcommand("context", help="Room admins: seed joiners' Bits chats with thread context")
    @command.argument("state")
    async def context_cmd(self, evt: MessageEvent, state: str) -> None:
        if state not in ("on", "off"):
            await evt.reply(f"Usage: `!{self.cmd} context on|off`")
            return
        if not await self._is_room_admin(evt.room_id, evt.sender):
            await evt.reply("Only room admins can change thread context.")
            return
        await self.room_settings.set_thread_context(evt.room_id, state == "on")
        await evt.reply(
            "Thread context on. When on, people joining a thread get their own Bits "
            "conversation seeded with the thread's visible messages; that context is stored "
            "in their Bits history." if state == "on" else
            "Thread context off. People joining a thread start with a plain question. "
            "(When on, people joining a thread get their own Bits conversation seeded with "
            "the thread's visible messages; that context is stored in their Bits history.)"
        )

    # ------------------------------------------------------------------ passive events

    @event.on(EventType.ROOM_MESSAGE)
    async def on_message(self, evt: MessageEvent) -> None:
        if evt.sender == self.client.mxid or evt.content.msgtype not in (
            MessageType.TEXT, MessageType.EMOTE
        ):
            return
        body = evt.content.body or ""
        if body.startswith("!"):
            return  # commands are handled by @command handlers
        if evt.content.get_edit():
            return
        if contains_redirect(body):
            await self._login_paste(evt, body)  # never forward a pasted code to Bits/unfurl
            return
        thread_parent = evt.content.get_thread_parent()
        self._spawn(self.unfurl.maybe_unfurl(evt.room_id, evt.event_id, evt.sender, body,
                                             thread_root=thread_parent))

        question = (
            extract_question(evt.content.serialize(), self.client.mxid, self._displayname)
            if self.config["chat.respond_to_mentions"] else None
        )

        # Follow-up in a thread that already has this user's Bits conversation.
        if thread_parent:
            # Others must mention the bot to join; that creates their own conversation.
            conv = await self.conversations.get(evt.room_id, thread_parent, evt.sender)
            if conv:
                await self._ask_guarded(evt.room_id, thread_parent, evt.sender,
                                        question or body)
                return
        if question is not None:
            if not question:
                await evt.reply(f"Ask me anything about your Datadog org, e.g. "
                                f"\"{self._displayname or 'bits'}: why is checkout slow?\" "
                                f"(`!{self.cmd} help` for more).")
                return
            await self._ask_guarded(evt.room_id, self._thread_root(evt), evt.sender, question)
            return
        if self.config["chat.respond_in_dms"] and await self._is_private_dm(evt.room_id,
                                                                           evt.sender):
            await self._ask_guarded(evt.room_id, self._thread_root(evt), evt.sender, body)

    @event.on(EventType.REACTION)
    async def on_reaction(self, evt: ReactionEvent) -> None:
        if evt.sender == self.client.mxid:
            return
        rel = evt.content.relates_to
        target, key = rel.event_id, rel.key
        try:
            if await self.chat.on_reaction(evt.room_id, target, evt.sender, key):
                return
            await self.alerts.on_reaction(evt.room_id, target, evt.sender, key,
                                          bot_mxid=self.client.mxid)
        except BackendError as e:
            await self.messenger.send(evt.room_id, _notice(str(e)), thread_root=target)

    # ------------------------------------------------------------------ web routes

    @web.get("/oauth/callback")
    async def oauth_callback(self, req: aioweb.Request) -> aioweb.Response:
        state, code = req.query.get("state", ""), req.query.get("code", "")
        if not state or not code:
            return self._page(400, "Sign-in not completed",
                              "Datadog didn't return an authorization. Return to Matrix and "
                              "try again.")
        try:
            done = await self.oauth.complete(state, code)
        except ValueError:
            return self._page(400, "Link expired",
                              "This sign-in link is invalid, expired, or already used. "
                              f"Run <code>!{self.cmd} login</code> again in Matrix.")
        except (UpstreamError, AuthRequired):
            return self._page(502, "Sign-in failed",
                              "Couldn't complete sign-in with Datadog. Try again shortly.")
        self.factory.forget(done.link.mxid)
        who = done.link.dd_user_handle or "identity unknown"
        try:
            await self.messenger.send(RoomID(done.dm_room_id), _notice(
                f"Linked as {who}. Not you? Run `!{self.cmd} logout` now."))
        except Exception:
            self.log.exception("Could not send link confirmation")
        # Name the Matrix account: if someone else started this sign-in and sent you the link,
        # this is where you notice that your Datadog access went to them.
        return self._page(200, "Signed in",
                          f"Your Datadog account is now linked to the Matrix account "
                          f"<b>{html.escape(done.link.mxid)}</b>. If that isn't you, revoke this "
                          f"app's access in your Datadog personal settings now. Otherwise you "
                          f"can close this tab and return to Matrix.")

    @staticmethod
    def _page(status: int, title: str, text: str) -> aioweb.Response:
        return aioweb.Response(status=status, text=_PAGE.format(title=title, text=text),
                               headers=_NO_STORE)

    @web.post("/hook/{token}")
    async def webhook(self, req: aioweb.Request) -> aioweb.Response:
        # read() may return a partial chunk; loop until EOF or just past the cap.
        buf = bytearray()
        while len(buf) <= MAX_WEBHOOK_BODY:
            chunk = await req.content.read(MAX_WEBHOOK_BODY + 1 - len(buf))
            if not chunk:
                break
            buf += chunk
        raw = bytes(buf)
        status = await self.alerts.handle_webhook(req.match_info["token"], raw)
        return aioweb.Response(status=status, headers={"Cache-Control": "no-store"})
