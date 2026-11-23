"""Investigation service: start, poll, steer and resume Bits AI investigations from Matrix.

Authorisation: every Datadog call uses ``factory.for_user(requested_by)`` — the person who
started the investigation — freshly on each poll (tokens refresh). Only that user may steer.

AuthRequired mid-poll: we post one notice in the thread, stop polling, and leave the row
*unfinished*. After the requester re-links, calling :meth:`InvestigationService.resume_open`
(or steering the investigation) restarts polling for it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable

from mautrix.types import EventID, RoomID, TextMessageEventContent

from ..backend.base import (
    AuthRequired,
    BackendError,
    BackendFactory,
    Investigation,
    InvestigationStatus,
    MonitorAlertRef,
    PermissionDenied,
    RateLimited,
)
from ..matrix.render import (
    render_error,
    render_investigation_result,
    render_investigation_status,
    render_notice,
)
from ..matrix.throttle import EditThrottle
from ..repos import InvestigationRepo, InvestigationRow
from ..sites import Site
from .base import Messenger

MAX_DESCRIPTION = 4096
BACKOFF = (10, 20, 40, 60)
ELAPSED_REFRESH_S = 120


def _notice(text: str) -> TextMessageEventContent:
    return render_notice(text)


class InvestigationService:
    def __init__(
        self,
        factory: BackendFactory,
        messenger: Messenger,
        repo: InvestigationRepo,
        site: Site,
        log: logging.Logger,
        *,
        max_minutes: int = 30,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.factory = factory
        self.messenger = messenger
        self.repo = repo
        self.site = site
        self.log = log
        self.max_s = max_minutes * 60
        self._clock = clock
        self._sleep = sleep
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopped = False

    # ------------------------------------------------------------------ start

    async def start_general(
        self, room_id: RoomID, thread_root: EventID, sender: str, description: str
    ) -> str | None:
        description = (description or "").strip()
        if not description:
            await self._say(room_id, thread_root, render_error("Describe what to investigate."))
            return None
        if len(description) > MAX_DESCRIPTION:
            await self._say(
                room_id,
                thread_root,
                render_error(
                    f"Description too long ({len(description)} > {MAX_DESCRIPTION} chars)."
                ),
            )
            return None
        return await self._start(
            room_id, thread_root, sender, lambda b: b.investigation_start_general(description)
        )

    async def start_from_alert(
        self, room_id: RoomID, thread_root: EventID, sender: str, alert: MonitorAlertRef
    ) -> str | None:
        return await self._start(
            room_id, thread_root, sender, lambda b: b.investigation_start_from_alert(alert)
        )

    async def _start(self, room_id, thread_root, sender, call) -> str | None:
        try:
            backend = await self.factory.for_user(sender)
            inv_id = await call(backend)
        except AuthRequired:
            await self.messenger.notify_login_needed(room_id, sender)
            return None
        except BackendError as e:
            await self._say(room_id, thread_root, render_error(str(e)))
            return None
        placeholder = Investigation(inv_id, InvestigationStatus.IN_PROGRESS, "in progress")
        event_id = await self.messenger.send(
            room_id, render_investigation_status(placeholder, 0), thread_root=thread_root
        )
        await self.repo.add(
            inv_id,
            room_id,
            thread_root,
            sender,
            status_event_id=event_id,
            started_at=int(self._clock()),
        )
        row = await self.repo.get(inv_id)
        self._spawn(row)
        return inv_id

    # ------------------------------------------------------------------ steer

    async def _find(self, room_id: str, thread_root: str) -> InvestigationRow | None:
        # InvestigationRepo has no (room, thread) lookup; small helper here (flagged in report).
        r = await self.repo.db.fetchrow(
            f"SELECT {self.repo._COLS} FROM investigation WHERE room_id=$1 AND thread_root=$2 "
            "ORDER BY started_at DESC LIMIT 1",
            room_id,
            thread_root,
        )
        return InvestigationRow(**dict(r)) if r else None

    async def steer(self, room_id: RoomID, thread_root: EventID, sender: str, message: str) -> None:
        row = await self._find(room_id, thread_root)
        if row is None:
            await self._say(room_id, thread_root, render_error("No investigation in this thread."))
            return
        if sender != row.requested_by:
            await self._say(
                room_id,
                thread_root,
                render_error("Only the person who started this investigation can steer it."),
            )
            return
        message = (message or "").strip()
        if not message:
            await self._say(room_id, thread_root, render_error("Say what to focus on."))
            return
        try:
            backend = await self.factory.for_user(row.requested_by)
            inv = await backend.investigation_get(row.investigation_id)
            if inv.status.terminal:
                await backend.investigation_resume(row.investigation_id, message)
                await self._reopen(row)
                row = await self.repo.get(row.investigation_id)
            else:
                await backend.investigation_steer(row.investigation_id, message)
        except AuthRequired:
            await self.messenger.notify_login_needed(room_id, sender)
            return
        except BackendError as e:
            await self._say(room_id, thread_root, render_error(str(e)))
            return
        await self._say(room_id, thread_root, _notice("Sent to the investigation."))
        if row.investigation_id not in self._tasks:
            self._spawn(row)

    async def _reopen(self, row: InvestigationRow) -> None:
        """Mark a finished row open again (fresh start time) and post a new status message."""
        placeholder = Investigation(
            row.investigation_id, InvestigationStatus.IN_PROGRESS, "in progress"
        )
        eid = await self.messenger.send(
            row.room_id, render_investigation_status(placeholder, 0), thread_root=row.thread_root
        )
        await self.repo.db.execute(
            "UPDATE investigation SET finished_at=NULL, last_status=$1, started_at=$2, "
            "status_event_id=$3 WHERE investigation_id=$4",
            "in progress",
            int(self._clock()),
            eid,
            row.investigation_id,
        )

    # ------------------------------------------------------------------ lifecycle

    async def resume_open(self) -> None:
        now = self._clock()
        for row in await self.repo.list_open():
            if row.investigation_id in self._tasks:
                continue
            if now - row.started_at >= self.max_s:
                await self._timed_out(row, None)
            else:
                self._spawn(row)

    async def stop(self) -> None:
        self._stopped = True  # rows stay open; resume_open() picks them up on next start
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def join(self) -> None:
        """Wait for all poll tasks to end (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # ------------------------------------------------------------------ polling

    def _spawn(self, row: InvestigationRow) -> None:
        if self._stopped:
            return
        iid = row.investigation_id
        task = asyncio.ensure_future(self._poll(row))
        self._tasks[iid] = task

        def _done(t: asyncio.Task, iid=iid) -> None:
            if self._tasks.get(iid) is t:
                del self._tasks[iid]

        task.add_done_callback(_done)

    async def _say(self, room_id, thread_root, content) -> None:
        await self.messenger.send(room_id, content, thread_root=thread_root)

    async def _timed_out(self, row: InvestigationRow, inv: Investigation | None) -> None:
        inv = inv or Investigation(
            row.investigation_id, InvestigationStatus.UNKNOWN, row.last_status
        )
        inv = dataclasses.replace(inv, raw_status="still running — check Datadog")
        if row.status_event_id:
            elapsed = int(self._clock() - row.started_at)
            try:
                await self.messenger.edit(
                    row.room_id,
                    EventID(row.status_event_id),
                    render_investigation_status(inv, elapsed),
                )
            except Exception:
                self.log.warning("timeout edit failed for %s", row.investigation_id)
        await self.repo.finish(row.investigation_id, row.last_status, int(self._clock()))

    async def _poll(self, row: InvestigationRow) -> None:
        iid = row.investigation_id
        throttle: EditThrottle | None = None
        if row.status_event_id:
            event_id = EventID(row.status_event_id)

            async def send_edit(content) -> None:
                # Progress edits are cosmetic: a homeserver hiccup must not end polling.
                try:
                    await self.messenger.edit(row.room_id, event_id, content)
                except Exception:
                    self.log.warning("status edit failed for investigation %s", iid)

            throttle = EditThrottle(send_edit, clock=self._clock)
        inv = Investigation(iid, InvestigationStatus.IN_PROGRESS, row.last_status or "in progress")
        shown = (inv.raw_status, inv.title)
        last_edit = self._clock()
        step = 0
        try:
            while True:
                delay: float = BACKOFF[min(step, len(BACKOFF) - 1)]
                step += 1
                await self._sleep(delay)
                if self._clock() - row.started_at >= self.max_s:
                    if throttle:
                        await throttle.aclose()
                    await self._timed_out(row, inv)
                    return
                try:
                    backend = await self.factory.for_user(row.requested_by)
                    inv = await backend.investigation_get(iid)
                except AuthRequired:
                    await self._say(
                        row.room_id,
                        row.thread_root,
                        render_error(
                            f"{row.requested_by} please log in again (DM me `!bits login`); "
                            "I'll resume tracking this investigation afterwards."
                        ),
                    )
                    return  # row stays open so resume_open()/steer can restart polling
                except PermissionDenied as e:
                    await self._say(row.room_id, row.thread_root, render_error(str(e)))
                    await self.repo.finish(iid, row.last_status, int(self._clock()))
                    return
                except RateLimited as e:
                    if e.retry_after:
                        await self._sleep(min(float(e.retry_after), 300))
                    continue
                except BackendError:
                    self.log.warning("transient error polling investigation %s", iid)
                    continue
                status_str = (
                    inv.status.value
                    if inv.status is not InvestigationStatus.UNKNOWN
                    else inv.raw_status
                )
                if status_str != row.last_status:
                    await self.repo.update_status(iid, status_str)
                    row.last_status = status_str
                elapsed = int(self._clock() - row.started_at)
                if inv.status.terminal:
                    if throttle:
                        await throttle.final(render_investigation_status(inv, elapsed))
                    try:
                        await self.messenger.send(
                            row.room_id,
                            render_investigation_result(inv, self.site),
                            thread_root=row.thread_root,
                        )
                    except Exception:
                        # Keep the row open and retry on the next poll (until the timeout).
                        self.log.warning("could not post result for investigation %s", iid)
                        continue
                    await self.repo.finish(iid, status_str, int(self._clock()))
                    return
                key = (inv.raw_status, inv.title)
                now = self._clock()
                if throttle and (key != shown or now - last_edit >= ELAPSED_REFRESH_S):
                    await throttle.update(render_investigation_status(inv, elapsed))
                    shown, last_edit = key, now
        except asyncio.CancelledError:
            raise
        except Exception:
            self.log.exception("investigation poll crashed for %s", iid)
        finally:
            if throttle:
                await throttle.aclose()
