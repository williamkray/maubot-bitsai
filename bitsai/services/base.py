"""Contract between feature services and Matrix (frozen).

Services never touch maubot/mautrix client objects directly; the plugin provides a
``Messenger``. This keeps services unit-testable with a fake (see tests/fakes.py).

Authorisation rules every service must follow:
- Datadog calls act as the **Matrix user who triggered them** (``BackendFactory.for_user``).
  Never reuse one user's backend for another user's request.
- Approving a Bits pending action, steering an investigation, or anything else that acts with
  someone's credential may only be done by that same user (the conversation/investigation owner).
- Errors shown in rooms come from ``str(BackendError)`` only — never raw exceptions/payloads.
- ``AuthRequired`` -> reply briefly in the room ("link your account: DM me `!bits login`") and
  let the plugin open the DM; never post sign-in links in shared rooms.
"""

from __future__ import annotations

from typing import Protocol

from mautrix.types import EventID, MessageEventContent, RoomID, UserID


class Messenger(Protocol):
    async def send(
        self,
        room_id: RoomID,
        content: MessageEventContent,
        *,
        thread_root: EventID | None = None,
        reply_to: EventID | None = None,
    ) -> EventID:
        """Send a message; ``thread_root`` puts it in that thread (m.thread, with fallback)."""
        ...

    async def edit(self, room_id: RoomID, event_id: EventID, content: MessageEventContent) -> None:
        ...

    async def react(self, room_id: RoomID, event_id: EventID, key: str) -> EventID: ...

    async def redact(self, room_id: RoomID, event_id: EventID, reason: str | None = None) -> None:
        ...

    async def set_typing(self, room_id: RoomID, typing: bool, timeout_ms: int = 30_000) -> None:
        ...

    async def get_thread_messages(
        self, room_id: RoomID, thread_root: EventID, limit: int = 30
    ) -> list[tuple[UserID, str]]:
        """Visible messages of a thread, oldest first, root included: ``(sender, plain body)``.
        Only text/notice messages; edits resolved to their latest content where possible.
        Returns [] on any error (context is best-effort)."""
        ...

    async def notify_login_needed(self, room_id: RoomID, user_id: UserID) -> None:
        """Tell ``user_id`` they must link Datadog (plugin opens a DM; never posts links here)."""
        ...
