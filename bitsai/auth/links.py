"""Persistence of user links and OAuth tokens, and the per-user token refresher.

Secrets are encrypted with :class:`SecretBox`; the AAD is ``mxid NUL column`` so ciphertexts
cannot be moved between users or columns.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
from mautrix.util.async_db import Database

from ..backend.base import AuthRequired, UpstreamError
from .base import Credential, CredentialKind, UserLink
from .crypto import SecretBox

REFRESH_MARGIN_S = 60


class InvalidGrant(Exception):
    """The authorization server rejected the refresh token (revoked/expired/unknown client)."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: int  # unix seconds

    def __repr__(self) -> str:
        return f"TokenSet(expires_at={self.expires_at})"


@dataclass(frozen=True)
class StoredTokens:
    kind: CredentialKind
    access_token: str
    refresh_token: str | None
    expires_at: int | None
    refresh_enc: str | None  # stored ciphertext, used for compare-and-swap updates

    def __repr__(self) -> str:
        return f"StoredTokens(kind={self.kind.value}, expires_at={self.expires_at})"


def _aad(mxid: str, column: str) -> str:
    return f"{mxid}\x00{column}"


def affected_rows(result: Any) -> int:
    """Rows affected by ``db.execute`` on either backend (sqlite Cursor / asyncpg status str)."""
    rowcount = getattr(result, "rowcount", None)
    if isinstance(rowcount, int):
        return rowcount
    if isinstance(result, str):
        try:
            return int(result.rsplit(" ", 1)[-1])
        except ValueError:
            return 0
    return 0


class LinkRepository:
    """Implements ``LinkStore`` plus encrypted OAuth token storage."""

    def __init__(self, db: Database, box: SecretBox, clock: Callable[[], float] = time.time):
        self._db = db
        self._box = box
        self._clock = clock

    def _row_to_link(self, row: Any) -> UserLink:
        return UserLink(
            mxid=row["mxid"],
            site=row["site"],
            kind=CredentialKind(row["kind"]),
            dd_user_handle=row["dd_user_handle"],
            linked_at=float(row["linked_at"]),
        )

    async def get_link(self, mxid: str) -> UserLink | None:
        row = await self._db.fetchrow(
            "SELECT mxid, site, kind, dd_user_handle, linked_at FROM user_link WHERE mxid=$1",
            mxid,
        )
        return self._row_to_link(row) if row else None

    async def _upsert(
        self,
        mxid: str,
        site: str,
        kind: CredentialKind,
        handle: str | None,
        access_token: str,
        refresh_token: str | None,
        expires_at: int | None,
    ) -> UserLink:
        if not access_token:
            raise ValueError("empty token")
        now = int(self._clock())
        access_enc = self._box.encrypt(access_token, _aad(mxid, "access_token_enc"))
        refresh_enc = (
            self._box.encrypt(refresh_token, _aad(mxid, "refresh_token_enc"))
            if refresh_token
            else None
        )
        await self._db.execute(
            """INSERT INTO user_link (mxid, site, kind, dd_user_handle, access_token_enc,
                                      refresh_token_enc, expires_at, linked_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (mxid) DO UPDATE SET
                 site=excluded.site, kind=excluded.kind, dd_user_handle=excluded.dd_user_handle,
                 access_token_enc=excluded.access_token_enc,
                 refresh_token_enc=excluded.refresh_token_enc,
                 expires_at=excluded.expires_at, linked_at=excluded.linked_at""",
            mxid,
            site,
            kind.value,
            handle,
            access_enc,
            refresh_enc,
            expires_at,
            now,
        )
        return UserLink(mxid, site, kind, handle, float(now))

    async def save_pat(
        self, mxid: str, site: str, token: str, dd_user_handle: str | None
    ) -> UserLink:
        return await self._upsert(mxid, site, CredentialKind.PAT, dd_user_handle, token, None, None)

    async def save_oauth(
        self, mxid: str, site: str, tokens: TokenSet, dd_user_handle: str | None
    ) -> UserLink:
        return await self._upsert(
            mxid,
            site,
            CredentialKind.OAUTH,
            dd_user_handle,
            tokens.access_token,
            tokens.refresh_token,
            tokens.expires_at,
        )

    async def delete(self, mxid: str) -> bool:
        res = await self._db.execute("DELETE FROM user_link WHERE mxid=$1", mxid)
        return affected_rows(res) > 0

    async def get_tokens(self, mxid: str) -> StoredTokens | None:
        row = await self._db.fetchrow(
            """SELECT kind, access_token_enc, refresh_token_enc, expires_at
               FROM user_link WHERE mxid=$1""",
            mxid,
        )
        if not row:
            return None
        refresh_enc = row["refresh_token_enc"]
        try:
            access = self._box.decrypt(row["access_token_enc"], _aad(mxid, "access_token_enc"))
            refresh = (
                self._box.decrypt(refresh_enc, _aad(mxid, "refresh_token_enc"))
                if refresh_enc
                else None
            )
        except ValueError:
            # Wrong key or tampered row: unusable. Don't delete (a key mixup shouldn't wipe
            # every link); the user is asked to log in again.
            raise AuthRequired(
                "Stored credentials could not be decrypted; please log in again."
            ) from None
        expires = row["expires_at"]
        return StoredTokens(
            kind=CredentialKind(row["kind"]),
            access_token=access,
            refresh_token=refresh,
            expires_at=int(expires) if expires is not None else None,
            refresh_enc=refresh_enc,
        )

    async def update_tokens(
        self,
        mxid: str,
        expected_refresh_enc: str | None,
        ts: TokenSet,
        fallback_refresh: str | None,
    ) -> bool:
        """Compare-and-swap: only update if the row still holds the refresh token we used
        (guards against a concurrent logout / re-login). Returns False if the row changed."""
        refresh = ts.refresh_token or fallback_refresh
        res = await self._db.execute(
            """UPDATE user_link SET access_token_enc=$1, refresh_token_enc=$2, expires_at=$3
               WHERE mxid=$4 AND kind='oauth' AND refresh_token_enc=$5""",
            self._box.encrypt(ts.access_token, _aad(mxid, "access_token_enc")),
            self._box.encrypt(refresh, _aad(mxid, "refresh_token_enc")) if refresh else None,
            ts.expires_at,
            mxid,
            expected_refresh_enc,
        )
        return affected_rows(res) == 1

    async def delete_if_unchanged(self, mxid: str, expected_refresh_enc: str | None) -> bool:
        if expected_refresh_enc is None:
            res = await self._db.execute(
                "DELETE FROM user_link WHERE mxid=$1 AND refresh_token_enc IS NULL", mxid
            )
        else:
            res = await self._db.execute(
                "DELETE FROM user_link WHERE mxid=$1 AND refresh_token_enc=$2",
                mxid,
                expected_refresh_enc,
            )
        return affected_rows(res) > 0


class _Refresher(Protocol):
    async def refresh(self, refresh_token: str) -> TokenSet: ...


class TokenManager:
    """Implements ``TokenProvider``."""

    def __init__(
        self,
        repo: LinkRepository,
        oauth: _Refresher,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo
        self._oauth = oauth
        self._clock = clock
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def _lock(self, mxid: str) -> asyncio.Lock:
        lock = self._locks.get(mxid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[mxid] = lock
        return lock

    def _fresh(self, tokens: StoredTokens) -> bool:
        return tokens.expires_at is None or tokens.expires_at - self._clock() >= REFRESH_MARGIN_S

    async def get(self, mxid: str) -> Credential:
        tokens = await self._repo.get_tokens(mxid)
        if tokens is None:
            raise AuthRequired("Not linked to Datadog. Use `!bits login`.")
        if tokens.kind is CredentialKind.PAT or self._fresh(tokens):
            return Credential(tokens.kind, tokens.access_token, tokens.expires_at)

        lock = self._lock(mxid)  # keep a strong ref for the duration
        async with lock:
            # Re-read: another task may have refreshed (or the user logged out) while we waited.
            tokens = await self._repo.get_tokens(mxid)
            if tokens is None:
                raise AuthRequired("Not linked to Datadog. Use `!bits login`.")
            if tokens.kind is CredentialKind.PAT or self._fresh(tokens):
                return Credential(tokens.kind, tokens.access_token, tokens.expires_at)
            if not tokens.refresh_token:
                await self._repo.delete_if_unchanged(mxid, tokens.refresh_enc)
                raise AuthRequired("Datadog session expired. Use `!bits login`.")
            try:
                ts = await self._oauth.refresh(tokens.refresh_token)
            except InvalidGrant:
                await self._repo.delete_if_unchanged(mxid, tokens.refresh_enc)
                raise AuthRequired("Datadog session expired. Use `!bits login`.") from None
            except UpstreamError:
                raise
            except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
                raise UpstreamError("Could not reach Datadog to refresh the session.") from None
            if not await self._repo.update_tokens(
                mxid, tokens.refresh_enc, ts, tokens.refresh_token
            ):
                raise AuthRequired("Datadog link changed during refresh. Use `!bits login`.")
            return Credential(CredentialKind.OAUTH, ts.access_token, ts.expires_at)
