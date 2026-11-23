"""Shared test doubles for services."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from mautrix.types import EventID


@dataclass
class Sent:
    room_id: str
    content: Any
    thread_root: str | None = None
    reply_to: str | None = None
    event_id: str = ""


@dataclass
class FakeMessenger:
    sent: list[Sent] = field(default_factory=list)
    edits: list[tuple[str, str, Any]] = field(default_factory=list)
    reactions: list[tuple[str, str, str]] = field(default_factory=list)
    redactions: list[tuple[str, str]] = field(default_factory=list)
    typing: list[tuple[str, bool]] = field(default_factory=list)
    login_needed: list[tuple[str, str]] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def _eid(self) -> EventID:
        return EventID(f"$ev{next(self._ids)}")

    async def send(self, room_id, content, *, thread_root=None, reply_to=None):
        eid = self._eid()
        self.sent.append(Sent(room_id, content, thread_root, reply_to, eid))
        return eid

    async def edit(self, room_id, event_id, content):
        self.edits.append((room_id, event_id, content))

    async def react(self, room_id, event_id, key):
        self.reactions.append((room_id, event_id, key))
        return self._eid()

    async def redact(self, room_id, event_id, reason=None):
        self.redactions.append((room_id, event_id))

    async def set_typing(self, room_id, typing, timeout_ms=30_000):
        self.typing.append((room_id, typing))

    thread_messages: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    async def get_thread_messages(self, room_id, thread_root, limit=30):
        return list(self.thread_messages.get(thread_root, []))[-limit:]

    async def notify_login_needed(self, room_id, user_id):
        self.login_needed.append((room_id, user_id))

    def last_body(self) -> str:
        if self.edits:
            return self.edits[-1][2].body
        return self.sent[-1].content.body


class FakeFactory:
    """BackendFactory returning a per-user backend; raises AuthRequired for unknown users."""

    def __init__(self, backends: dict[str, Any]):
        self.backends = backends
        self.calls: list[str] = []

    async def for_user(self, mxid):
        from bitsai.backend.base import AuthRequired

        self.calls.append(mxid)
        if mxid not in self.backends:
            raise AuthRequired("Not linked to Datadog. Use `!bits login`.")
        return self.backends[mxid]
