"""Chat with Bits AI in Matrix threads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mautrix.types import EventID, RoomID, TextMessageEventContent, UserID

from ..backend.base import (
    AuthRequired,
    BackendError,
    BackendFactory,
    BitsBackend,
    ChatStatus,
    ChatTurn,
)
from ..matrix.render import (
    render_chat_answer,
    render_error,
    render_notice,
    render_pending_action,
    render_thinking,
)
from ..repos import ConversationRepo, RoomSettingsRepo
from ..sites import Site
from .base import Messenger

PENDING_TTL_S = 15 * 60
APPROVE = "✅"
DENY = "❌"
GENERIC_ERROR = "Something went wrong talking to Bits AI. Please try again."
CONTEXT_LIMIT = 30
CONTEXT_MAX_CHARS = 6000
OMITTED = "[earlier messages omitted]"
_BOT_NOTICE_PREFIXES = ("Bits AI is thinking", "⚠️", "Still working on the previous question")


def build_context_message(
    messages: list[tuple[str, str]], sender: str, text: str, max_chars: int = CONTEXT_MAX_CHARS
) -> str | None:
    """Wrap ``text`` with a preamble of the thread's prior messages, or None if nothing to add.

    Excludes the triggering message and the bot's thinking/error notices; keeps the most recent
    lines when over ``max_chars``."""
    msgs = list(messages)
    if msgs:
        last_sender, last_body = msgs[-1]
        lb, tb = (last_body or "").strip(), text.strip()
        if last_sender == sender and (lb == tb or (tb and lb.endswith(tb))):
            msgs.pop()
    lines = [
        f"{who}: {body.strip()}"
        for who, body in msgs
        if body and body.strip() and not body.strip().startswith(_BOT_NOTICE_PREFIXES)
    ]
    if not lines:
        return None
    joined = "\n".join(lines)
    if len(joined) > max_chars:
        budget = max_chars - len(OMITTED) - 1
        while len(lines) > 1 and len("\n".join(lines)) > budget:
            lines.pop(0)
        joined = "\n".join(lines)[-budget:]
        joined = f"{OMITTED}\n{joined}"
    return (
        "Context: the following is a shared Matrix thread I'm part of. Messages from other "
        "people and earlier AI answers are included for context only.\n---\n"
        f"{joined}\n---\nMy question: {text}"
    )


@dataclass
class _Pending:
    room_id: str
    thread_root: str
    owner: str
    conversation_id: str
    action_id: str
    created: float


def _notice(text: str) -> TextMessageEventContent:
    return render_notice(text)


class ChatService:
    def __init__(
        self,
        factory: BackendFactory,
        messenger: Messenger,
        conversations: ConversationRepo,
        site: Site,
        log: Any,
        *,
        timeout_s: float = 180,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        settings: RoomSettingsRepo | None = None,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.messenger = messenger
        self.conversations = conversations
        self.site = site
        self.log = log
        self.timeout_s = timeout_s
        self.clock = clock
        self.sleep = sleep
        self._busy: set[tuple[str, str, str]] = set()
        self._pending: dict[str, _Pending] = {}

    async def stop(self) -> None:
        self._pending.clear()

    # ------------------------------------------------------------------ entry points

    async def ask(self, room_id: RoomID, thread_root: EventID, sender: UserID, text: str) -> None:
        key = (str(room_id), str(thread_root), str(sender))
        if key in self._busy:
            await self.messenger.send(
                room_id, _notice("Still working on the previous question…"), thread_root=thread_root
            )
            return
        self._busy.add(key)
        try:
            # Each person's conversation always runs with their OWN credential.
            conv = await self.conversations.get(room_id, thread_root, sender)
            try:
                backend = await self.factory.for_user(sender)
            except AuthRequired:
                await self.messenger.notify_login_needed(room_id, sender)
                return
            except Exception:
                self.log.exception("for_user failed")
                await self.messenger.send(
                    room_id, render_error(GENERIC_ERROR), thread_root=thread_root
                )
                return
            owners = await self.conversations.owners(room_id, thread_root)
            shared = any(o != sender for o in owners)
            message = text
            if conv is None:
                message = await self._seed(room_id, thread_root, sender, text)
            await self._run(
                backend,
                room_id,
                thread_root,
                sender,
                message,
                conv.bits_conversation_id if conv else None,
                None,
                shared,
            )
        finally:
            self._busy.discard(key)

    async def _seed(
        self, room_id: RoomID, thread_root: EventID, sender: UserID, text: str
    ) -> str:
        """First message of a new conversation: prepend visible thread context if enabled."""
        try:
            if self.settings is not None:
                if not (await self.settings.get(room_id)).thread_context_enabled:
                    return text
            msgs = await self.messenger.get_thread_messages(
                room_id, thread_root, limit=CONTEXT_LIMIT
            )
            return build_context_message(msgs, sender, text) or text
        except Exception:
            self.log.exception("thread context failed")
            return text

    async def on_reaction(
        self, room_id: RoomID, target_event_id: EventID, sender: UserID, key: str
    ) -> bool:
        self._expire()
        pend = self._pending.get(str(target_event_id))
        if pend is None or pend.room_id != str(room_id):
            return False
        if sender != pend.owner or key not in (APPROVE, DENY):
            return True  # ours, but ignored
        bkey = (pend.room_id, pend.thread_root, pend.owner)
        if bkey in self._busy:
            return True
        self._pending.pop(str(target_event_id), None)  # single-use
        self._busy.add(bkey)
        try:
            try:
                backend = await self.factory.for_user(pend.owner)
            except AuthRequired:
                await self.messenger.notify_login_needed(room_id, sender)
                return True
            except Exception:
                self.log.exception("for_user failed")
                await self.messenger.send(
                    room_id, render_error(GENERIC_ERROR), thread_root=EventID(pend.thread_root)
                )
                return True
            owners = await self.conversations.owners(pend.room_id, pend.thread_root)
            await self._run(
                backend,
                room_id,
                EventID(pend.thread_root),
                sender,
                "Approved." if key == APPROVE else "Denied.",
                pend.conversation_id,
                pend.action_id,
                any(o != pend.owner for o in owners),
            )
        finally:
            self._busy.discard(bkey)
        return True

    # ------------------------------------------------------------------ internals

    def _expire(self) -> None:
        now = self.clock()
        for k in [k for k, p in self._pending.items() if now - p.created > PENDING_TTL_S]:
            del self._pending[k]

    async def _run(
        self,
        backend: BitsBackend,
        room_id: RoomID,
        thread_root: EventID,
        owner: UserID,
        text: str,
        conversation_id: str | None,
        pending_action_id: str | None,
        shared: bool = False,
    ) -> None:
        placeholder = await self.messenger.send(
            room_id, render_thinking(str(owner) if shared else None), thread_root=thread_root
        )
        await self.messenger.set_typing(room_id, True)
        try:
            turn = await backend.chat_send(text, conversation_id, pending_action_id)
            await self.conversations.put(room_id, thread_root, owner, turn.conversation_id)
            turn = await self._wait(backend, turn)
            await self._finish(room_id, thread_root, owner, placeholder, turn, shared)
        except BackendError as e:
            await self._safe_edit(room_id, placeholder, render_error(str(e)))
        except Exception:
            self.log.exception("chat failed")
            await self._safe_edit(room_id, placeholder, render_error(GENERIC_ERROR))
        finally:
            try:
                await self.messenger.set_typing(room_id, False)
            except Exception:
                self.log.exception("typing off failed")

    async def _safe_edit(self, room_id: RoomID, event_id: EventID, content: Any) -> None:
        try:
            await self.messenger.edit(room_id, event_id, content)
        except Exception:
            self.log.exception("edit failed")

    async def _wait(self, backend: BitsBackend, turn: ChatTurn) -> ChatTurn | None:
        """Poll until terminal / requires_action. Returns None on timeout."""
        start = self.clock()
        while not (turn.status.terminal or turn.status == ChatStatus.REQUIRES_ACTION):
            if self.clock() - start >= self.timeout_s:
                return None
            t0 = self.clock()
            turn = await backend.chat_poll(turn.conversation_id, turn.task_id)
            if turn.status.terminal or turn.status == ChatStatus.REQUIRES_ACTION:
                break
            wait = turn.poll_after or 0.0
            if self.clock() - t0 < 1.0:
                wait = max(wait, 1.0)
            if wait > 0:
                await self.sleep(wait)
        return turn

    async def _finish(
        self,
        room_id: RoomID,
        thread_root: EventID,
        owner: UserID,
        placeholder: EventID,
        turn: ChatTurn | None,
        shared: bool = False,
    ) -> None:
        if turn is None:
            await self.messenger.edit(
                room_id, placeholder, render_error("Bits AI took too long to answer. Try again.")
            )
        elif turn.status == ChatStatus.COMPLETED:
            answer = turn.message or ""
            if shared:
                answer = f"**→ {owner}**\n\n{answer}"
            await self.messenger.edit(room_id, placeholder, render_chat_answer(answer, self.site))
        elif turn.status == ChatStatus.REQUIRES_ACTION and turn.pending_action:
            await self.messenger.edit(
                room_id, placeholder, render_pending_action(turn.pending_action, self.site)
            )
            self._pending[str(placeholder)] = _Pending(
                str(room_id),
                str(thread_root),
                str(owner),
                turn.conversation_id,
                turn.pending_action.id,
                self.clock(),
            )
            await self.messenger.react(room_id, placeholder, APPROVE)
            await self.messenger.react(room_id, placeholder, DENY)
        elif turn.status == ChatStatus.REQUIRES_ACTION:
            await self.messenger.edit(
                room_id, placeholder, render_error("Bits AI asked for an action I can't show.")
            )
        else:
            msg = (
                "Bits AI cancelled the request."
                if turn.status == ChatStatus.CANCELLED
                else (turn.error or "Bits AI could not answer.")
            )
            await self.messenger.edit(room_id, placeholder, render_error(msg))
