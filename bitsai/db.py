"""Database schema (frozen contract). Repositories live next to their feature modules.

SQL must run on both SQLite and Postgres: ``$n`` placeholders, TEXT/BIGINT/BOOLEAN only,
timestamps are unix seconds (BIGINT). Secrets are stored only in ``*_enc`` columns as
AES-GCM blobs produced by ``bitsai.auth.crypto`` (base64 TEXT).
"""

from __future__ import annotations

from mautrix.util.async_db import Connection, UpgradeTable

upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial schema")
async def upgrade_v1(conn: Connection) -> None:
    await conn.execute(
        """CREATE TABLE user_link (
            mxid              TEXT PRIMARY KEY,
            site              TEXT NOT NULL,
            kind              TEXT NOT NULL,      -- 'oauth' | 'pat'
            dd_user_handle    TEXT,
            access_token_enc  TEXT NOT NULL,
            refresh_token_enc TEXT,
            expires_at        BIGINT,             -- NULL = unknown/none (PAT)
            linked_at         BIGINT NOT NULL
        )"""
    )
    await conn.execute(
        """CREATE TABLE oauth_state (
            state             TEXT PRIMARY KEY,   -- random, single-use
            mxid              TEXT NOT NULL,
            dm_room_id        TEXT NOT NULL,      -- where to confirm the link
            pkce_verifier_enc TEXT NOT NULL,
            created_at        BIGINT NOT NULL
        )"""
    )
    await conn.execute(
        """CREATE TABLE conversation (
            room_id              TEXT NOT NULL,
            thread_root          TEXT NOT NULL,
            bits_conversation_id TEXT NOT NULL,
            owner_mxid           TEXT NOT NULL,
            created_at           BIGINT NOT NULL,
            PRIMARY KEY (room_id, thread_root)
        )"""
    )
    await conn.execute(
        """CREATE TABLE investigation (
            investigation_id TEXT PRIMARY KEY,
            room_id          TEXT NOT NULL,
            thread_root      TEXT NOT NULL,
            status_event_id  TEXT,
            requested_by     TEXT NOT NULL,
            last_status      TEXT NOT NULL,
            started_at       BIGINT NOT NULL,
            finished_at      BIGINT
        )"""
    )
    await conn.execute(
        """CREATE TABLE room_settings (
            room_id            TEXT PRIMARY KEY,
            webhook_token_hash TEXT,              -- sha256 hex of the per-room secret
            unfurl_enabled     BOOLEAN NOT NULL DEFAULT false,
            alert_format       TEXT NOT NULL DEFAULT 'full'
        )"""
    )
    await conn.execute(
        """CREATE TABLE alert_message (
            event_id     TEXT PRIMARY KEY,        -- Matrix event of the posted alert
            room_id      TEXT NOT NULL,
            monitor_id   BIGINT NOT NULL,
            alert_event_id TEXT NOT NULL,         -- Datadog event id (>=16 digits)
            event_ts_ms  BIGINT NOT NULL,
            dedupe_key   TEXT NOT NULL,
            created_at   BIGINT NOT NULL
        )"""
    )
    await conn.execute("CREATE INDEX alert_message_dedupe ON alert_message (room_id, dedupe_key)")
    await conn.execute("CREATE INDEX investigation_open ON investigation (finished_at)")


@upgrade_table.register(description="Per-person Bits conversations in shared threads")
async def upgrade_v2(conn: Connection) -> None:
    # A Matrix thread can now hold one Bits conversation per participant: each person's
    # conversation runs with their own Datadog identity.
    await conn.execute(
        """CREATE TABLE conversation_v2 (
            room_id              TEXT NOT NULL,
            thread_root          TEXT NOT NULL,
            owner_mxid           TEXT NOT NULL,
            bits_conversation_id TEXT NOT NULL,
            created_at           BIGINT NOT NULL,
            PRIMARY KEY (room_id, thread_root, owner_mxid)
        )"""
    )
    await conn.execute(
        """INSERT INTO conversation_v2
               (room_id, thread_root, owner_mxid, bits_conversation_id, created_at)
           SELECT room_id, thread_root, owner_mxid, bits_conversation_id, created_at
           FROM conversation"""
    )
    await conn.execute("DROP TABLE conversation")
    await conn.execute("ALTER TABLE conversation_v2 RENAME TO conversation")
    # Seed a newcomer's conversation with the thread's visible messages (admins can disable).
    await conn.execute(
        "ALTER TABLE room_settings ADD COLUMN thread_context_enabled BOOLEAN NOT NULL "
        "DEFAULT true"
    )
