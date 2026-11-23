"""Link unfurling: post resource cards for Datadog URLs shared in opted-in rooms.

Cards are fetched with the *poster's* credential, so a preview never exposes data the poster
cannot already see. Unfurls are off by default per room.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import urlsplit

from mautrix.types import EventID, RoomID

from bitsai.backend.base import AuthRequired, BackendFactory, ResourceKind
from bitsai.matrix.render import render_resource_card
from bitsai.repos import RoomSettingsRepo
from bitsai.services.base import Messenger
from bitsai.sites import SUPPORTED_SITES, Site

_URL_RE = re.compile(r"https?://[^\s<>\"'\\^`{|}]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>"
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_PATHS: tuple[tuple[ResourceKind, re.Pattern[str]], ...] = (
    (ResourceKind.MONITOR, re.compile(r"^/monitors/(\d+)/?$")),
    (
        ResourceKind.DASHBOARD,
        re.compile(r"^/dashboard/([a-z0-9]{3}-[a-z0-9]{3}-[a-z0-9]{3})(?:/[^/]*)?/?$"),
    ),
    (ResourceKind.INCIDENT, re.compile(r"^/incidents/(\d+)/?$")),
    (ResourceKind.NOTEBOOK, re.compile(r"^/notebook/(\d+)(?:/[^/]*)?/?$")),
)
_BLOCKED_PREFIXES = {"api", "mcp"}


def _host_ok(host: str, site: Site) -> bool:
    suffix = "." + site.domain
    if host in SUPPORTED_SITES or not host.endswith(suffix):
        return False
    prefix = host[: -len(suffix)]
    return bool(_LABEL_RE.match(prefix)) and prefix not in _BLOCKED_PREFIXES


def find_links(text: str, site: Site, max_links: int = 3) -> list[tuple[ResourceKind, str]]:
    out: list[tuple[ResourceKind, str]] = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(_TRAILING)
        try:
            u = urlsplit(url)
            port = u.port
        except ValueError:
            continue
        if u.scheme != "https" or u.username is not None or u.password is not None or port:
            continue
        host = (u.hostname or "").lower()
        if not _host_ok(host, site):
            continue
        for kind, rx in _PATHS:
            pm = rx.match(u.path.lower() if kind is ResourceKind.DASHBOARD else u.path)
            if pm:
                item = (kind, pm.group(1))
                if item not in out:
                    out.append(item)
                break
        if len(out) >= max_links:
            break
    return out[:max_links]


class UnfurlService:
    RATE_MAX = 5
    RATE_WINDOW_S = 60.0

    def __init__(
        self,
        factory: BackendFactory,
        messenger: Messenger,
        settings: RoomSettingsRepo,
        site: Site,
        log,
        *,
        default_enabled: bool = False,
        cache_ttl_s: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory = factory
        self.messenger = messenger
        self.settings = settings
        self.site = site
        self.log = log
        self.default_enabled = default_enabled
        self.cache_ttl_s = cache_ttl_s
        self.clock = clock
        # key includes sender: results are scoped to that user's permissions
        self._cache: dict[tuple[str, ResourceKind, str], tuple[float, object]] = {}
        self._sent: dict[str, deque[float]] = {}

    async def set_enabled(self, room_id: str, enabled: bool) -> None:
        await self.settings.set_unfurl(room_id, enabled)

    async def is_enabled(self, room_id: str) -> bool:
        if not self.default_enabled:
            return (await self.settings.get(room_id)).unfurl_enabled
        row = await self.settings.db.fetchrow(
            "SELECT unfurl_enabled FROM room_settings WHERE room_id=$1", room_id
        )
        return True if row is None else bool(row["unfurl_enabled"])

    def _rate_ok(self, room_id: str) -> bool:
        now = self.clock()
        q = self._sent.setdefault(room_id, deque())
        while q and now - q[0] >= self.RATE_WINDOW_S:
            q.popleft()
        return len(q) < self.RATE_MAX

    async def maybe_unfurl(
        self,
        room_id: RoomID,
        event_id: EventID,
        sender: str,
        body: str,
        thread_root: EventID | None = None,
    ) -> None:
        links = find_links(body, self.site)
        if not links or not await self.is_enabled(room_id):
            return
        try:
            backend = await self.factory.for_user(sender)
        except AuthRequired:
            return
        except Exception as e:  # noqa: BLE001
            self.log.debug("unfurl: backend unavailable: %s", type(e).__name__)
            return
        for kind, rid in links:
            if not self._rate_ok(room_id):
                return
            key = (sender, kind, rid)
            now = self.clock()
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_ttl_s:
                summary = hit[1]
            else:
                try:
                    summary = await backend.resource_get(kind, rid)
                except Exception as e:  # noqa: BLE001  (permission/not found/other: skip)
                    self.log.debug("unfurl: skip %s %s: %s", kind.value, rid, type(e).__name__)
                    continue
                self._cache[key] = (now, summary)
            try:
                await self.messenger.send(
                    room_id,
                    render_resource_card(summary),  # type: ignore[arg-type]
                    thread_root=thread_root,
                    reply_to=event_id,
                )
            except Exception as e:  # noqa: BLE001
                self.log.debug("unfurl: send failed: %s", type(e).__name__)
                return
            self._sent.setdefault(room_id, deque()).append(self.clock())
