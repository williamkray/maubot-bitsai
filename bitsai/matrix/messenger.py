"""mautrix-backed implementation of ``services.base.Messenger``."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from mautrix.api import Method, Path
from mautrix.client import Client
from mautrix.types import (
    Event,
    EventID,
    EventType,
    MessageEvent,
    MessageEventContent,
    MessageType,
    RoomID,
    UserID,
)

from .render import render_notice

NOTIFY_COOLDOWN_S = 600


class MatrixMessenger:
    def __init__(
        self,
        client: Client,
        command: str,
        open_dm: Callable[[UserID], Awaitable[tuple[RoomID | None, bool]]],
        is_private_dm: Callable[[RoomID, UserID], Awaitable[bool]],
    ) -> None:
        self._client = client
        self._command = command
        self._open_dm = open_dm
        self._is_private_dm = is_private_dm
        self._notified: dict[UserID, float] = {}

    async def send(
        self,
        room_id: RoomID,
        content: MessageEventContent,
        *,
        thread_root: EventID | None = None,
        reply_to: EventID | None = None,
    ) -> EventID:
        if thread_root:
            content.set_thread_parent(thread_root)
            if reply_to:
                content.relates_to.in_reply_to = None
                content.set_reply(reply_to)
                content.relates_to.is_falling_back = False
        elif reply_to:
            content.set_reply(reply_to)
        return await self._client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)

    async def edit(self, room_id: RoomID, event_id: EventID, content: MessageEventContent) -> None:
        content.set_edit(event_id)
        await self._client.send_message_event(room_id, EventType.ROOM_MESSAGE, content)

    async def react(self, room_id: RoomID, event_id: EventID, key: str) -> EventID:
        return await self._client.react(room_id, event_id, key)

    async def redact(self, room_id: RoomID, event_id: EventID, reason: str | None = None) -> None:
        await self._client.redact(room_id, event_id, reason=reason)

    async def set_typing(self, room_id: RoomID, typing: bool, timeout_ms: int = 30_000) -> None:
        await self._client.set_typing(room_id, timeout=timeout_ms if typing else 0)

    async def get_thread_messages(
        self, room_id: RoomID, thread_root: EventID, limit: int = 30
    ) -> list[tuple[UserID, str]]:
        """Best-effort: root + latest ``limit`` thread messages, oldest first. [] on error."""
        try:
            out: list[tuple[UserID, str]] = []
            root = await self._client.get_event(room_id, thread_root)
            parsed = await self._parse_event(root)
            if parsed:
                out.append(parsed)
            data = await self._client.api.request(
                Method.GET,
                Path.v1.rooms[room_id].relations[thread_root]["m.thread"],
                query_params={"limit": str(limit), "dir": "b"},
            )
            chunk = list((data or {}).get("chunk", []))
            chunk.reverse()  # dir=b is newest first
            for raw in chunk:
                try:
                    parsed = await self._parse_event(Event.deserialize(raw), raw)
                except Exception:
                    continue
                if parsed:
                    out.append(parsed)
            return out
        except Exception:
            return []

    async def _parse_event(
        self, evt: Any, raw: dict | None = None
    ) -> tuple[UserID, str] | None:
        if evt is None:
            return None
        if evt.type == EventType.ROOM_ENCRYPTED:
            crypto = getattr(self._client, "crypto", None)
            if not crypto:
                return None
            try:
                evt = await crypto.decrypt_megolm_event(evt)
            except Exception:
                return None
        if not isinstance(evt, MessageEvent) or evt.type != EventType.ROOM_MESSAGE:
            return None
        content = evt.content
        if getattr(content, "msgtype", None) not in (
            MessageType.TEXT,
            MessageType.NOTICE,
            MessageType.EMOTE,
        ):
            return None
        if content.get_edit():
            return None
        body = content.body or ""
        # Prefer the latest edit's content when the server bundled it.
        try:
            rel = ((raw or {}).get("unsigned") or {}).get("m.relations", {}).get("m.replace")
            new = ((rel or {}).get("content") or {}).get("m.new_content") or {}
            if isinstance(new.get("body"), str):
                body = new["body"]
        except Exception:
            pass
        return evt.sender, body

    async def notify_login_needed(self, room_id: RoomID, user_id: UserID) -> None:
        login = f"`!{self._command} login`"
        if await self._is_private_dm(room_id, user_id):
            # Always answer in a DM; the user would otherwise see silence.
            text = f"Your Matrix account isn't linked to Datadog yet. Run {login} here."
        else:
            # Throttle reminders in shared rooms to avoid spamming everyone.
            # No 0.0 default: monotonic time starts near zero on a freshly booted host, so
            # "never notified" would look recent and the first reminder would be dropped.
            now = time.monotonic()
            last = self._notified.get(user_id)
            if last is not None and now - last < NOTIFY_COOLDOWN_S:
                return
            self._notified[user_id] = now
            dm, created = await self._open_dm(user_id)
            if dm is None:
                where = f"send me a direct message with {login}"
            elif created:
                where = f"I've invited you to a direct chat; run {login} there"
            else:
                where = f"run {login} in our direct chat"
                await self._notice(dm, f"Run {login} here to link your Datadog account.")
            text = f"{user_id}: your Matrix account isn't linked to Datadog yet; {where}."
        await self._notice(room_id, text)

    async def _notice(self, room_id: RoomID, text: str) -> None:
        await self._client.send_message_event(
            room_id,
            EventType.ROOM_MESSAGE,
            render_notice(text),
        )
