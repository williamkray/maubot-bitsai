"""Coalescing throttle for Matrix message edits."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


class EditThrottle:
    def __init__(
        self,
        send_edit: Callable[[Any], Awaitable[None]],
        min_interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._send = send_edit
        self._min = min_interval
        self._clock = clock
        self._last: float | None = None
        self._pending: Any = None
        self._has_pending = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _do_send(self, content: Any) -> None:
        async with self._lock:
            self._last = self._clock()
            await self._send(content)

    async def update(self, content: Any) -> None:
        if self._closed:
            return
        self._pending, self._has_pending = content, True
        if self._task and not self._task.done():
            return  # the scheduled task will pick up the newest content
        wait = 0.0
        if self._last is not None:
            wait = max(0.0, self._last + self._min - self._clock())
        if wait <= 0:
            self._has_pending = False
            await self._do_send(content)
        else:
            self._task = asyncio.ensure_future(self._delayed(wait))

    async def _delayed(self, wait: float) -> None:
        await asyncio.sleep(wait)
        if not self._has_pending or self._closed:
            return
        content, self._pending, self._has_pending = self._pending, None, False
        await self._do_send(content)

    async def final(self, content: Any) -> None:
        await self._cancel()
        self._has_pending, self._pending = False, None
        await self._do_send(content)

    async def _cancel(self) -> None:
        task, self._task = self._task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def aclose(self) -> None:
        self._closed = True
        await self._cancel()
