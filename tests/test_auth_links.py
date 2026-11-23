import asyncio
import base64
import logging

import aiohttp
import pytest
from mautrix.util.async_db import Database

import bitsai.db
from bitsai.auth.base import CredentialKind
from bitsai.auth.crypto import SecretBox
from bitsai.auth.links import InvalidGrant, LinkRepository, TokenManager, TokenSet
from bitsai.backend.base import AuthRequired, UpstreamError

from .test_auth_oauth import Clock

MXID = "@alice:example.org"


@pytest.fixture
async def db():
    d = Database.create(
        "sqlite::memory:",
        upgrade_table=bitsai.db.upgrade_table,
        log=logging.getLogger("test.db"),
    )
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
def box():
    return SecretBox(base64.b64encode(b"k" * 32).decode())


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(db, box, clock):
    return LinkRepository(db, box, clock)


class FakeOAuth:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.exc: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def refresh(self, refresh_token):
        self.calls += 1
        if self.gate:
            await self.gate.wait()
        if self.exc:
            raise self.exc
        return TokenSet(f"NEW-ACCESS-{self.calls}", None, int(self.clock() + 3600))


async def link_oauth(repo, clock, expires_in=3600, refresh="OLD-REFRESH-SECRET"):
    return await repo.save_oauth(
        MXID,
        "datadoghq.com",
        TokenSet("OLD-ACCESS-SECRET", refresh, int(clock() + expires_in)),
        "h",
    )


async def test_pat_roundtrip_encrypted_and_never_refreshes(repo, db, clock):
    link = await repo.save_pat(MXID, "datadoghq.com", "PAT-SECRET", "me")
    assert link.kind is CredentialKind.PAT and link.linked_at == int(clock())
    row = await db.fetchrow("SELECT * FROM user_link WHERE mxid=$1", MXID)
    assert "PAT-SECRET" not in row["access_token_enc"] and row["expires_at"] is None
    oauth = FakeOAuth(clock)
    clock.t += 10**8
    cred = await TokenManager(repo, oauth, clock).get(MXID)
    assert cred.kind is CredentialKind.PAT and cred.access_token == "PAT-SECRET"
    assert cred.expires_at is None and oauth.calls == 0
    assert "PAT-SECRET" not in repr(cred)


async def test_get_link_delete(repo):
    assert await repo.get_link(MXID) is None
    await repo.save_pat(MXID, "datadoghq.com", "t", None)
    assert (await repo.get_link(MXID)).site == "datadoghq.com"
    assert await repo.delete(MXID) is True
    assert await repo.delete(MXID) is False
    assert await repo.get_link(MXID) is None


async def test_relogin_replaces_link(repo):
    await repo.save_pat(MXID, "datadoghq.com", "t", None)
    await repo.save_oauth(MXID, "datadoghq.eu", TokenSet("a", "r", 5), "x")
    link = await repo.get_link(MXID)
    assert link.kind is CredentialKind.OAUTH and link.site == "datadoghq.eu"


async def test_unlinked_raises(repo, clock):
    with pytest.raises(AuthRequired):
        await TokenManager(repo, FakeOAuth(clock), clock).get(MXID)


async def test_fresh_token_no_refresh(repo, clock):
    await link_oauth(repo, clock, expires_in=120)
    oauth = FakeOAuth(clock)
    cred = await TokenManager(repo, oauth, clock).get(MXID)
    assert cred.access_token == "OLD-ACCESS-SECRET" and oauth.calls == 0


async def test_refresh_when_near_expiry(repo, clock):
    await link_oauth(repo, clock, expires_in=59)
    oauth = FakeOAuth(clock)
    tm = TokenManager(repo, oauth, clock)
    cred = await tm.get(MXID)
    assert cred.access_token == "NEW-ACCESS-1" and cred.expires_at == int(clock()) + 3600
    stored = await repo.get_tokens(MXID)
    assert stored.access_token == "NEW-ACCESS-1"
    assert stored.refresh_token == "OLD-REFRESH-SECRET"  # not rotated: kept
    await tm.get(MXID)
    assert oauth.calls == 1


async def test_concurrent_get_single_refresh(repo, clock):
    await link_oauth(repo, clock, expires_in=10)
    oauth = FakeOAuth(clock)
    oauth.gate = asyncio.Event()
    tm = TokenManager(repo, oauth, clock)
    tasks = [asyncio.create_task(tm.get(MXID)) for _ in range(8)]
    await asyncio.sleep(0.05)
    oauth.gate.set()
    creds = await asyncio.gather(*tasks)
    assert oauth.calls == 1
    assert {c.access_token for c in creds} == {"NEW-ACCESS-1"}


async def test_locks_are_per_user(repo, clock):
    await link_oauth(repo, clock, expires_in=10)
    await repo.save_oauth("@bob:x", "datadoghq.com", TokenSet("B", "BR", int(clock() + 10)), None)
    oauth = FakeOAuth(clock)
    tm = TokenManager(repo, oauth, clock)
    await asyncio.gather(tm.get(MXID), tm.get("@bob:x"))
    assert oauth.calls == 2


async def test_invalid_grant_deletes_link(repo, clock):
    await link_oauth(repo, clock, expires_in=10)
    oauth = FakeOAuth(clock)
    oauth.exc = InvalidGrant()
    with pytest.raises(AuthRequired):
        await TokenManager(repo, oauth, clock).get(MXID)
    assert await repo.get_link(MXID) is None


@pytest.mark.parametrize("exc", [UpstreamError("x"), TimeoutError(), aiohttp.ClientError()])
async def test_network_error_keeps_link(repo, clock, exc):
    await link_oauth(repo, clock, expires_in=10)
    oauth = FakeOAuth(clock)
    oauth.exc = exc
    with pytest.raises(UpstreamError):
        await TokenManager(repo, oauth, clock).get(MXID)
    assert await repo.get_link(MXID) is not None


async def test_expired_without_refresh_token(repo, clock):
    await link_oauth(repo, clock, expires_in=10, refresh=None)
    with pytest.raises(AuthRequired):
        await TokenManager(repo, FakeOAuth(clock), clock).get(MXID)
    assert await repo.get_link(MXID) is None


async def test_relogin_during_refresh_not_clobbered(repo, clock):
    await link_oauth(repo, clock, expires_in=10)
    oauth = FakeOAuth(clock)
    oauth.gate = asyncio.Event()
    tm = TokenManager(repo, oauth, clock)
    task = asyncio.create_task(tm.get(MXID))
    await asyncio.sleep(0.05)
    await repo.save_oauth(MXID, "datadoghq.com", TokenSet("NEWLOGIN", "NEWLOGIN-R", 10**10), "h")
    oauth.gate.set()
    with pytest.raises(AuthRequired):
        await task
    assert (await repo.get_tokens(MXID)).access_token == "NEWLOGIN"


async def test_swapped_ciphertext_rejected(repo, db, clock):
    await link_oauth(repo, clock)
    await repo.save_oauth("@bob:x", "datadoghq.com", TokenSet("BOB", "BOBR", 10**10), None)
    enc = await db.fetchval("SELECT access_token_enc FROM user_link WHERE mxid=$1", MXID)
    await db.execute("UPDATE user_link SET access_token_enc=$1 WHERE mxid=$2", enc, "@bob:x")
    with pytest.raises(AuthRequired):
        await repo.get_tokens("@bob:x")
    # column swap
    renc = await db.fetchval("SELECT refresh_token_enc FROM user_link WHERE mxid=$1", MXID)
    await db.execute("UPDATE user_link SET access_token_enc=$1 WHERE mxid=$2", renc, MXID)
    with pytest.raises(AuthRequired):
        await repo.get_tokens(MXID)


async def test_no_secrets_in_logs_or_reprs(repo, clock, caplog):
    caplog.set_level(logging.DEBUG)
    await link_oauth(repo, clock, expires_in=10)
    oauth = FakeOAuth(clock)
    tm = TokenManager(repo, oauth, clock)
    cred = await tm.get(MXID)
    stored = await repo.get_tokens(MXID)
    link = await repo.get_link(MXID)
    blob = caplog.text + "".join(map(repr, (cred, stored, link, repo, tm))) + str(cred)
    for s in ("OLD-ACCESS-SECRET", "OLD-REFRESH-SECRET", "NEW-ACCESS-1"):
        assert s not in blob
