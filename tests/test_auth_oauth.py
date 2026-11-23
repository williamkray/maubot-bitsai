import base64
import hashlib
import logging
import urllib.parse

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from mautrix.util.async_db import Database

import bitsai.db
from bitsai.auth.base import CredentialKind
from bitsai.auth.crypto import SecretBox
from bitsai.auth.links import InvalidGrant, LinkRepository
from bitsai.auth.oauth import CompletedLink, DatadogOAuth, pkce_challenge
from bitsai.backend.base import UpstreamError
from bitsai.sites import Site

ACCESS = "ACCESS-TOKEN-SECRET-aaa"
REFRESH = "REFRESH-TOKEN-SECRET-bbb"
SECRETS = (ACCESS, REFRESH, "NEWACCESS-SECRET", "AUTH-CODE-SECRET")
REDIRECT = "https://bot.example.org/_matrix/maubot/plugin/x/callback"
MXID = "@alice:example.org"
DM = "!dm:example.org"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeDatadog:
    """Fake discovery/DCR/token/current_user server."""

    def __init__(self, fixtures):
        self.fx = fixtures
        self.calls: list[tuple[str, str]] = []
        self.dcr_bodies: list[dict] = []
        self.token_forms: list[dict] = []
        self.token_status = 200
        self.token_error = None
        self.token_delay = None
        self.user_status = 200
        self.origin = ""
        self.discovery_ok = True
        self.app = web.Application()
        r = self.app.router
        r.add_get("/.well-known/oauth-protected-resource/v1/mcp", self.prm)
        r.add_get("/.well-known/oauth-authorization-server/v1/mcp", self.asm)
        r.add_post("/register", self.register)
        r.add_post("/token", self.token)
        r.add_get("/api/v2/current_user", self.user)

    async def prm(self, req):
        self.calls.append(("GET", "prm"))
        if not self.discovery_ok:
            return web.Response(status=404)
        return web.json_response({"authorization_servers": [self.origin + "/v1/mcp"]})

    async def asm(self, req):
        self.calls.append(("GET", "asm"))
        m = dict(self.fx("oauth_metadata.json")["authorization_server"])
        m["issuer"] = self.origin + "/v1/mcp"
        m["authorization_endpoint"] = self.origin + "/authorize"
        m["token_endpoint"] = self.origin + "/token"
        m["registration_endpoint"] = self.origin + "/register"
        return web.json_response(m)

    async def register(self, req):
        self.calls.append(("POST", "register"))
        body = await req.json()
        self.dcr_bodies.append(body)
        out = dict(self.fx("dcr_0b_A.json")["body"])
        out["redirect_uris"] = body["redirect_uris"]
        return web.json_response(out, status=201)

    async def token(self, req):
        self.calls.append(("POST", "token"))
        form = dict(await req.post())
        self.token_forms.append(form)
        if self.token_delay:
            await self.token_delay()
        if self.token_status != 200:
            body = {"error": self.token_error or "invalid_grant", "error_description": REFRESH}
            return web.json_response(body, status=self.token_status)
        if form["grant_type"] == "authorization_code":
            if form["code"] != "AUTH-CODE-SECRET":
                return web.json_response({"error": "invalid_grant"}, status=400)
            body = {**self.fx("oauth_token_response.json")["body"]}
            body.update(access_token=ACCESS, refresh_token=REFRESH)
        else:
            body = {**self.fx("oauth_refresh_response.json")["body"]}
            body.update(access_token="NEWACCESS-SECRET", refresh_token=form["refresh_token"])
        return web.json_response(body)

    async def user(self, req):
        self.calls.append(("GET", "current_user"))
        assert req.headers["Authorization"] == f"Bearer {ACCESS}"
        if self.user_status != 200:
            return web.Response(status=self.user_status)
        return web.json_response(
            {"data": {"attributes": {"handle": "alice@dd.example", "email": "e@dd.example"}}}
        )


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
async def dd(load_fixture):
    fake = FakeDatadog(load_fixture)
    server = TestServer(fake.app)
    await server.start_server()
    fake.origin = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
async def http():
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def oauth(http, db, box, dd, clock):
    return DatadogOAuth(
        http,
        db,
        box,
        Site("datadoghq.com"),
        REDIRECT,
        "Bits for Matrix",
        logging.getLogger("test.oauth"),
        clock,
        _test_origin=dd.origin,
    )


def qs(url):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


async def test_begin_builds_url_and_runs_dcr(oauth, dd, db):
    url = await oauth.begin(MXID, dm_room_id=DM)
    q = qs(url)
    assert url.startswith(dd.origin + "/authorize?")
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == REDIRECT
    assert q["resource"] == "https://mcp.datadoghq.com/v1/mcp"
    assert q["scope"] == "mcp_all"
    assert q["client_id"] == "51ea5567-28b8-4c79-9a7d-e70239b14811"
    assert len(q["state"]) >= 43  # >=256 bits urlsafe
    body = dd.dcr_bodies[0]
    assert body["redirect_uris"] == [REDIRECT]
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["response_types"] == ["code"]
    row = await db.fetchrow("SELECT * FROM oauth_state WHERE state=$1", q["state"])
    assert row["mxid"] == MXID and row["dm_room_id"] == DM
    assert q["state"] not in row["pkce_verifier_enc"]


async def test_dcr_before_each_begin_and_metadata_cached(oauth, dd, clock):
    await oauth.begin(MXID, dm_room_id=DM)
    await oauth.begin(MXID, dm_room_id=DM)
    assert len(dd.dcr_bodies) == 2
    assert dd.calls.count(("GET", "prm")) == 1
    clock.t += 3601
    await oauth.begin(MXID, dm_room_id=DM)
    assert dd.calls.count(("GET", "prm")) == 2


async def test_pkce_challenge_matches_verifier(oauth, dd, db, box):
    url = await oauth.begin(MXID, dm_room_id=DM)
    q = qs(url)
    await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    form = dd.token_forms[0]
    assert form["code_verifier"] != q["code_challenge"]
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
    assert pkce_challenge(form["code_verifier"]) == q["code_challenge"]
    assert len(form["code_verifier"]) >= 43


async def test_complete_stores_encrypted_tokens(oauth, dd, db, clock):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    res = await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    assert isinstance(res, CompletedLink) and res.dm_room_id == DM
    assert res.link.mxid == MXID and res.link.kind is CredentialKind.OAUTH
    assert res.link.dd_user_handle == "alice@dd.example"
    form = dd.token_forms[0]
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == REDIRECT
    assert form["resource"] == "https://mcp.datadoghq.com/v1/mcp"
    assert form["client_id"] == q["client_id"]
    row = await db.fetchrow("SELECT * FROM user_link WHERE mxid=$1", MXID)
    assert ACCESS not in row["access_token_enc"] and REFRESH not in row["refresh_token_enc"]
    assert row["expires_at"] == int(clock.t) + 3600
    assert "AUTH-CODE" not in repr(res) and ACCESS not in repr(res)


async def test_current_user_failure_is_best_effort(oauth, dd):
    dd.user_status = 500
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    res = await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    assert res.link.dd_user_handle is None


async def test_state_single_use(oauth):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    with pytest.raises(ValueError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET")


async def test_state_consumed_even_if_exchange_fails(oauth, dd):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    with pytest.raises(UpstreamError):
        await oauth.complete(q["state"], "wrong-code")
    with pytest.raises(ValueError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET")


async def test_state_expiry(oauth, clock, db):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    clock.t += 601
    with pytest.raises(ValueError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    assert (
        await LinkRepository(db, SecretBox(base64.b64encode(b"k" * 32).decode())).get_link(MXID)
        is None
    )


async def test_state_valid_at_ttl_boundary(oauth, clock):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    clock.t += 599
    await oauth.complete(q["state"], "AUTH-CODE-SECRET")


@pytest.mark.parametrize("bad", ["", "nope", "x" * 5000, None])
async def test_wrong_state(oauth, bad):
    await oauth.begin(MXID, dm_room_id=DM)
    with pytest.raises(ValueError):
        await oauth.complete(bad, "AUTH-CODE-SECRET")


async def test_expired_states_purged_and_new_begin_supersedes(oauth, clock, db):
    await oauth.begin("@old:example.org", dm_room_id=DM)
    clock.t += 700
    s1 = qs(await oauth.begin(MXID, dm_room_id=DM))["state"]
    assert await db.fetchval("SELECT COUNT(*) FROM oauth_state") == 1
    s2 = qs(await oauth.begin(MXID, dm_room_id=DM))["state"]
    with pytest.raises(ValueError):
        await oauth.complete(s1, "AUTH-CODE-SECRET")
    await oauth.complete(s2, "AUTH-CODE-SECRET")


async def test_state_bound_to_requesting_mxid(oauth, db):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    res = await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    assert res.link.mxid == MXID


async def test_paste_binding_rejects_other_user_or_room_without_consuming(oauth):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    with pytest.raises(ValueError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET", mxid="@eve:x", dm_room_id=DM)
    with pytest.raises(ValueError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET", mxid=MXID, dm_room_id="!other:x")
    res = await oauth.complete(q["state"], "AUTH-CODE-SECRET", mxid=MXID, dm_room_id=DM)
    assert res.link.mxid == MXID


async def test_cancel_consumes_only_with_matching_binding(oauth):
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    assert not await oauth.cancel(q["state"], mxid="@eve:x", dm_room_id=DM)
    assert await oauth.cancel(q["state"], mxid=MXID, dm_room_id=DM)
    with pytest.raises(ValueError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    assert not await oauth.cancel(q["state"], mxid=MXID, dm_room_id=DM)


async def test_verifier_aad_bound_to_state(oauth, db):
    a = qs(await oauth.begin("@a:x", dm_room_id=DM))["state"]
    b = qs(await oauth.begin("@b:x", dm_room_id=DM))["state"]
    ra = await db.fetchval("SELECT pkce_verifier_enc FROM oauth_state WHERE state=$1", a)
    await db.execute("UPDATE oauth_state SET pkce_verifier_enc=$1 WHERE state=$2", ra, b)
    with pytest.raises(ValueError):
        await oauth.complete(b, "AUTH-CODE-SECRET")


async def test_refresh(oauth, dd):
    await oauth.begin(MXID, dm_room_id=DM)
    ts = await oauth.refresh(REFRESH)
    assert ts.access_token == "NEWACCESS-SECRET" and ts.refresh_token == REFRESH
    assert "NEWACCESS" not in repr(ts) and REFRESH not in repr(ts)
    form = dd.token_forms[-1]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == REFRESH


@pytest.mark.parametrize(
    "status,err,exc",
    [
        (400, "invalid_grant", InvalidGrant),
        (401, "whatever", InvalidGrant),
        (400, "invalid_request", UpstreamError),
        (500, "server_error", UpstreamError),
    ],
)
async def test_refresh_errors(oauth, dd, status, err, exc):
    dd.token_status, dd.token_error = status, err
    with pytest.raises(exc) as ei:
        await oauth.refresh(REFRESH)
    assert REFRESH not in str(ei.value)


async def test_discovery_fails_closed(oauth, dd):
    dd.discovery_ok = False
    with pytest.raises(UpstreamError):
        await oauth.begin(MXID, dm_room_id=DM)
    assert ("POST", "register") not in dd.calls


async def test_network_error_is_upstream(http, db, box, clock):
    o = DatadogOAuth(
        http,
        db,
        box,
        Site("datadoghq.com"),
        REDIRECT,
        "n",
        logging.getLogger("t"),
        clock,
        _test_origin="http://127.0.0.1:1",
    )
    with pytest.raises(UpstreamError):
        await o.ensure_client()


async def test_untrusted_endpoints_rejected(http, db, box, clock):
    """Production mode: metadata pointing off the Datadog domain is refused."""
    o = DatadogOAuth(
        http, db, box, Site("datadoghq.com"), REDIRECT, "n", logging.getLogger("t"), clock
    )
    assert not o._trusted("https://evil.example/token")
    assert not o._trusted("http://app.datadoghq.com/token")
    assert not o._trusted("https://app.datadoghq.com.evil.example/token")
    assert not o._trusted("https://notdatadoghq.com/token")
    assert not o._trusted("https://user@app.datadoghq.com/token")
    assert o._trusted("https://app.datadoghq.com/api/v2/oauth2/token")


async def test_revoke_instructions(oauth):
    text = oauth.revoke_instructions(Site("datadoghq.eu"))
    assert "datadoghq.eu" in text and "revoke" in text.lower()


async def test_no_secrets_in_logs_or_reprs(oauth, dd, caplog):
    caplog.set_level(logging.DEBUG)
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    dd.token_status = 400
    with pytest.raises(UpstreamError):
        await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    dd.token_status = 200
    q = qs(await oauth.begin(MXID, dm_room_id=DM))
    res = await oauth.complete(q["state"], "AUTH-CODE-SECRET")
    ts = await oauth.refresh(REFRESH)
    dd.token_status = 401
    with pytest.raises(InvalidGrant):
        await oauth.refresh(REFRESH)
    # The DB driver logs bound parameters at DEBUG, which includes the (single-use, already
    # consumed) state; secrets (tokens, code, verifier) must not appear anywhere.
    logs = "\n".join(r.getMessage() for r in caplog.records if r.name != "aiosqlite")
    assert q["state"] not in logs
    blob = caplog.text + repr(res) + str(res) + repr(ts) + str(ts) + repr(oauth)
    for s in SECRETS:
        assert s not in blob
    assert q["state"] not in repr(res)


def _bare_oauth(site):
    return DatadogOAuth(None, None, None, site, REDIRECT, "n", logging.getLogger("t"))


def test_authorize_url_uses_custom_subdomain():
    o = _bare_oauth(Site("datadoghq.com", "example-org"))
    assert (o._authorize_endpoint("https://app.datadoghq.com/oauth2/v1/authorize")
            == "https://example-org.datadoghq.com/oauth2/v1/authorize")


def test_authorize_url_unchanged_without_subdomain_or_foreign_host():
    assert (_bare_oauth(Site("datadoghq.eu"))._authorize_endpoint(
        "https://app.datadoghq.eu/oauth2/v1/authorize")
        == "https://app.datadoghq.eu/oauth2/v1/authorize")
    o = _bare_oauth(Site("datadoghq.com", "example-org"))
    assert (o._authorize_endpoint("https://login.datadoghq.com/authorize")
            == "https://login.datadoghq.com/authorize")


@pytest.mark.parametrize("bad", ["evil.com#", "a.b", "-x", "UPPER", "x" * 64, "a/b"])
def test_site_rejects_bad_subdomain(bad):
    with pytest.raises(ValueError):
        Site("datadoghq.com", bad)
