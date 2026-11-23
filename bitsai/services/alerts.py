"""Datadog webhook -> Matrix alert posts, with reaction-driven follow-ups.

Inbound webhooks are unauthenticated internet traffic: the only credential is the secret URL
token (stored as sha256 only). Every failure before the token check passes returns the same 404.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from mautrix.types import EventID, RoomID, UserID

from ..backend.base import MonitorAlertRef
from ..matrix.render import render_alert, render_notice
from ..repos import AlertMessageRepo, RoomSettingsRepo
from ..sites import Site
from .base import Messenger

MAX_PAYLOAD_BYTES = 64 * 1024
RATE_LIMIT_PER_MIN = 30
DEDUPE_WINDOW_S = 600
INVESTIGATE_KEY = "🔍"
ASK_KEY = "💬"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_STR_LIMITS = {
    "alert_id": 64,
    "title": 300,
    "status": 100,
    "transition": 100,
    "link": 2000,
    "snapshot_url": 2000,
}
_MAX_TAGS = 50
_MAX_TAG_LEN = 200

SETUP_TEMPLATE = """{
  "alert_id": "$ALERT_ID",
  "monitor_id": "$ALERT_ID",
  "event_id": "$ID",
  "event_ts": "$DATE",
  "title": "$EVENT_TITLE",
  "status": "$ALERT_STATUS",
  "transition": "$ALERT_TRANSITION",
  "link": "$LINK",
  "snapshot_url": "$SNAPSHOT",
  "tags": "$TAGS"
}"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _str(v: Any, limit: int) -> str:
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v).strip()[:limit]


def _parse_ts(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    n: float | None = None
    if isinstance(v, (int, float)):
        n = float(v)
    elif isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            n = float(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
    if n is None or n <= 0 or n != n or n > 1e17:
        return None
    return int(n) if n >= 1e11 else int(n * 1000)


def parse_payload(raw: bytes) -> dict:
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload too large")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
        raise ValueError("invalid JSON") from e
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    out: dict[str, Any] = {}
    for k, limit in _STR_LIMITS.items():
        val = _str(data.get(k), limit)
        if val:
            out[k] = val
    mid = data.get("monitor_id")
    if isinstance(mid, str) and mid.strip().isdigit() and len(mid.strip()) <= 18:
        out["monitor_id"] = int(mid.strip())
    elif isinstance(mid, int) and not isinstance(mid, bool) and 0 < mid < 10**18:
        out["monitor_id"] = mid
    eid = data.get("event_id")
    if isinstance(eid, int) and not isinstance(eid, bool):
        eid = str(eid)
    if isinstance(eid, str) and eid.strip().isascii() and eid.strip().isdigit():
        if 16 <= len(eid.strip()) <= 32:
            out["event_id"] = eid.strip()
    ts = _parse_ts(data.get("event_ts"))
    if ts is not None:
        out["event_ts"] = ts
    tags = data.get("tags")
    if isinstance(tags, str):
        tags = tags.split(",")
    if isinstance(tags, list):
        clean = [
            _str(t, _MAX_TAG_LEN)
            for t in tags
            if isinstance(t, (str, int, float)) and not isinstance(t, bool)
        ]
        out["tags"] = [t for t in clean if t][:_MAX_TAGS]
    return out


class AlertService:
    def __init__(
        self,
        messenger: Messenger,
        settings: RoomSettingsRepo,
        alerts: AlertMessageRepo,
        site: Site,
        log: Any,
        *,
        on_investigate: Callable[[RoomID, EventID, UserID, MonitorAlertRef], Awaitable[None]],
        on_ask: Callable[[RoomID, EventID, UserID, str], Awaitable[None]],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.messenger = messenger
        self.settings = settings
        self.alerts = alerts
        self.site = site
        self.log = log
        self.on_investigate = on_investigate
        self.on_ask = on_ask
        self.clock = clock
        self._tasks: set[asyncio.Task] = set()
        self._hits: dict[str, deque[float]] = {}
        self._seen: dict[tuple[str, str], float] = {}

    # ---- admin ----
    async def create_webhook(self, room_id: RoomID, webhook_base: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        await self.settings.set_webhook_token_hash(room_id, _hash(token))
        url = f"{webhook_base.rstrip('/')}/hook/{token}"
        text = (
            "**Datadog alert webhook created.**\n\n"
            f"Webhook URL: `{url}`\n\n"
            "**Keep this URL secret** — anyone who has it can post alerts into this room. "
            "Re-run the command to rotate it (the old URL stops working immediately).\n\n"
            "In Datadog, open Integrations -> Webhooks, add a webhook with that URL, and use "
            "this payload:\n\n```json\n" + SETUP_TEMPLATE + "\n```\n\n"
            "Then add `@webhook-<name>` to your monitor notification message. "
            "React with 🔍 to an alert to start a Bits investigation, or 💬 to ask Bits about it."
        )
        return token, text

    async def revoke_webhook(self, room_id: RoomID) -> None:
        await self.settings.set_webhook_token_hash(room_id, None)

    # ---- inbound ----
    def _rate_ok(self, room_id: str) -> bool:
        now = self.clock()
        q = self._hits.setdefault(room_id, deque())
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        return True

    async def handle_webhook(self, token: str, raw: bytes) -> int:
        if not isinstance(token, str) or not _TOKEN_RE.match(token):
            return 404
        room = await self.settings.find_by_webhook_hash(_hash(token))
        if room is None:
            return 404
        try:
            payload = parse_payload(raw)
        except ValueError:
            return 400
        if not self._rate_ok(room.room_id):
            return 429
        key = (
            f"{payload.get('alert_id', '')}:{payload.get('transition', '')}:"
            f"{payload.get('event_id') or payload.get('event_ts', '')}"
        )
        now = self.clock()
        self._seen = {k: t for k, t in self._seen.items() if t > now - DEDUPE_WINDOW_S}
        if (room.room_id, key) in self._seen or await self.alerts.seen_dedupe(
            room.room_id, key, DEDUPE_WINDOW_S
        ):
            return 202
        self._seen[(room.room_id, key)] = now
        task = asyncio.create_task(self._post(RoomID(room.room_id), payload, key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return 202

    async def _post(self, room_id: RoomID, payload: dict, key: str) -> None:
        try:
            content = render_alert(payload, self.site)
            eid = await self.messenger.send(room_id, content)
            if all(k in payload for k in ("monitor_id", "event_id", "event_ts")):
                await self.alerts.add(
                    eid,
                    room_id,
                    payload["monitor_id"],
                    payload["event_id"],
                    payload["event_ts"],
                    key,
                )
            for emoji in (INVESTIGATE_KEY, ASK_KEY):
                await self.messenger.react(room_id, eid, emoji)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.log.warning("failed to post alert: %s", type(e).__name__)

    # ---- reactions ----
    async def on_reaction(
        self,
        room_id: RoomID,
        target_event_id: EventID,
        sender: UserID,
        key: str,
        bot_mxid: UserID | None = None,
    ) -> bool:
        if key not in (INVESTIGATE_KEY, ASK_KEY):
            return False
        if bot_mxid is not None and sender == bot_mxid:
            return False
        msg = await self.alerts.get_by_event(target_event_id)
        if msg is None or msg.room_id != room_id:
            return False
        if key == INVESTIGATE_KEY:
            if not (msg.monitor_id and msg.alert_event_id and msg.event_ts_ms):
                await self._reply(room_id, target_event_id, "This alert lacks event data.")
                return True
            ref = MonitorAlertRef(msg.monitor_id, msg.alert_event_id, msg.event_ts_ms)
            await self.on_investigate(room_id, target_event_id, sender, ref)
            return True
        text = (
            f"Help me understand this Datadog alert: monitor {msg.monitor_id}. "
            "What is likely wrong and what should I check?"
        )
        await self.on_ask(room_id, target_event_id, sender, text)
        return True

    async def _reply(self, room_id: RoomID, thread_root: EventID, text: str) -> None:
        await self.messenger.send(
            room_id,
            render_notice(text),
            thread_root=thread_root,
        )

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
