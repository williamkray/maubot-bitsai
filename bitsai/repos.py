"""Small async repositories over the schema in :mod:`bitsai.db`."""

from __future__ import annotations

import time
from dataclasses import dataclass

from mautrix.util.async_db import Database


def _now() -> int:
    return int(time.time())


@dataclass
class Conversation:
    room_id: str
    thread_root: str
    bits_conversation_id: str
    owner_mxid: str
    created_at: int


class ConversationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, room_id: str, thread_root: str, owner_mxid: str) -> Conversation | None:
        row = await self.db.fetchrow(
            "SELECT room_id, thread_root, bits_conversation_id, owner_mxid, created_at "
            "FROM conversation WHERE room_id=$1 AND thread_root=$2 AND owner_mxid=$3",
            room_id,
            thread_root,
            owner_mxid,
        )
        return Conversation(**dict(row)) if row else None

    async def put(
        self,
        room_id: str,
        thread_root: str,
        owner_mxid: str,
        bits_conversation_id: str,
        created_at: int | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO conversation (room_id, thread_root, owner_mxid, bits_conversation_id, "
            "created_at) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (room_id, thread_root, owner_mxid) DO UPDATE SET "
            "bits_conversation_id=excluded.bits_conversation_id",
            room_id,
            thread_root,
            owner_mxid,
            bits_conversation_id,
            created_at if created_at is not None else _now(),
        )

    async def owners(self, room_id: str, thread_root: str) -> list[str]:
        rows = await self.db.fetch(
            "SELECT owner_mxid FROM conversation WHERE room_id=$1 AND thread_root=$2 "
            "ORDER BY created_at",
            room_id,
            thread_root,
        )
        return [r["owner_mxid"] for r in rows]

    async def has_any(self, room_id: str, thread_root: str) -> bool:
        return bool(await self.owners(room_id, thread_root))


@dataclass
class InvestigationRow:
    investigation_id: str
    room_id: str
    thread_root: str
    status_event_id: str | None
    requested_by: str
    last_status: str
    started_at: int
    finished_at: int | None


class InvestigationRepo:
    _COLS = (
        "investigation_id, room_id, thread_root, status_event_id, requested_by, "
        "last_status, started_at, finished_at"
    )

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        investigation_id: str,
        room_id: str,
        thread_root: str,
        requested_by: str,
        last_status: str = "in progress",
        status_event_id: str | None = None,
        started_at: int | None = None,
    ) -> None:
        await self.db.execute(
            f"INSERT INTO investigation ({self._COLS}) VALUES ($1,$2,$3,$4,$5,$6,$7,NULL)",
            investigation_id,
            room_id,
            thread_root,
            status_event_id,
            requested_by,
            last_status,
            started_at if started_at is not None else _now(),
        )

    async def get(self, investigation_id: str) -> InvestigationRow | None:
        row = await self.db.fetchrow(
            f"SELECT {self._COLS} FROM investigation WHERE investigation_id=$1", investigation_id
        )
        return InvestigationRow(**dict(row)) if row else None

    async def update_status(self, investigation_id: str, last_status: str) -> None:
        await self.db.execute(
            "UPDATE investigation SET last_status=$1 WHERE investigation_id=$2",
            last_status,
            investigation_id,
        )

    async def set_status_event(self, investigation_id: str, event_id: str) -> None:
        await self.db.execute(
            "UPDATE investigation SET status_event_id=$1 WHERE investigation_id=$2",
            event_id,
            investigation_id,
        )

    async def finish(
        self, investigation_id: str, last_status: str, finished_at: int | None = None
    ) -> None:
        await self.db.execute(
            "UPDATE investigation SET last_status=$1, finished_at=$2 WHERE investigation_id=$3",
            last_status,
            finished_at if finished_at is not None else _now(),
            investigation_id,
        )

    async def list_open(self) -> list[InvestigationRow]:
        rows = await self.db.fetch(
            f"SELECT {self._COLS} FROM investigation WHERE finished_at IS NULL ORDER BY started_at"
        )
        return [InvestigationRow(**dict(r)) for r in rows]


@dataclass
class RoomSettings:
    room_id: str
    webhook_token_hash: str | None = None
    unfurl_enabled: bool = False
    alert_format: str = "full"
    thread_context_enabled: bool = True


class RoomSettingsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, room_id: str) -> RoomSettings:
        row = await self.db.fetchrow(
            "SELECT room_id, webhook_token_hash, unfurl_enabled, alert_format, "
            "thread_context_enabled "
            "FROM room_settings WHERE room_id=$1",
            room_id,
        )
        if not row:
            return RoomSettings(room_id)
        return RoomSettings(
            row["room_id"],
            row["webhook_token_hash"],
            bool(row["unfurl_enabled"]),
            row["alert_format"],
            bool(row["thread_context_enabled"]),
        )

    async def _ensure(self, room_id: str) -> None:
        await self.db.execute(
            "INSERT INTO room_settings (room_id) VALUES ($1) ON CONFLICT (room_id) DO NOTHING",
            room_id,
        )

    async def set_webhook_token_hash(self, room_id: str, token_hash: str | None) -> None:
        await self._ensure(room_id)
        await self.db.execute(
            "UPDATE room_settings SET webhook_token_hash=$1 WHERE room_id=$2",
            token_hash,
            room_id,
        )

    async def set_unfurl(self, room_id: str, enabled: bool) -> None:
        await self._ensure(room_id)
        await self.db.execute(
            "UPDATE room_settings SET unfurl_enabled=$1 WHERE room_id=$2", enabled, room_id
        )

    async def set_thread_context(self, room_id: str, enabled: bool) -> None:
        await self._ensure(room_id)
        await self.db.execute(
            "UPDATE room_settings SET thread_context_enabled=$1 WHERE room_id=$2",
            enabled,
            room_id,
        )

    async def find_by_webhook_hash(self, token_hash: str) -> RoomSettings | None:
        row = await self.db.fetchrow(
            "SELECT room_id FROM room_settings WHERE webhook_token_hash=$1", token_hash
        )
        return await self.get(row["room_id"]) if row else None


@dataclass
class AlertMessage:
    event_id: str
    room_id: str
    monitor_id: int
    alert_event_id: str
    event_ts_ms: int
    dedupe_key: str
    created_at: int


class AlertMessageRepo:
    _COLS = "event_id, room_id, monitor_id, alert_event_id, event_ts_ms, dedupe_key, created_at"

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        event_id: str,
        room_id: str,
        monitor_id: int,
        alert_event_id: str,
        event_ts_ms: int,
        dedupe_key: str,
        created_at: int | None = None,
    ) -> None:
        await self.db.execute(
            f"INSERT INTO alert_message ({self._COLS}) VALUES ($1,$2,$3,$4,$5,$6,$7)",
            event_id,
            room_id,
            monitor_id,
            alert_event_id,
            event_ts_ms,
            dedupe_key,
            created_at if created_at is not None else _now(),
        )

    async def get_by_event(self, event_id: str) -> AlertMessage | None:
        row = await self.db.fetchrow(
            f"SELECT {self._COLS} FROM alert_message WHERE event_id=$1", event_id
        )
        return AlertMessage(**dict(row)) if row else None

    async def seen_dedupe(self, room_id: str, key: str, within_s: int) -> bool:
        row = await self.db.fetchrow(
            "SELECT 1 AS x FROM alert_message WHERE room_id=$1 AND dedupe_key=$2 "
            "AND created_at >= $3 LIMIT 1",
            room_id,
            key,
            _now() - within_s,
        )
        return row is not None
