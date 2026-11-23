"""Datadog MCP OAuth (discovery, DCR, PKCE S256, code exchange, refresh).

Security notes:
- Discovery fails closed; every endpoint taken from metadata must be https on the Datadog site's
  domain, so a poisoned document cannot redirect codes/verifiers elsewhere.
- No redirects are followed when talking to Datadog; response bodies are size-capped.
- Tokens, codes, verifiers and states are never logged or put in exception messages. Only
  HTTP status and a sanitised OAuth ``error`` code are reported.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from mautrix.util.async_db import Database

from ..backend.base import UpstreamError
from ..sites import Site
from .base import UserLink
from .crypto import SecretBox
from .links import InvalidGrant, LinkRepository, TokenSet, affected_rows

STATE_TTL_S = 600
METADATA_TTL_S = 3600
MAX_BODY = 1 << 20
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_ERROR_CODE = re.compile(r"^[a-z_]{1,40}$")
_REVOKED_CODES = {"invalid_grant", "invalid_client", "unauthorized_client"}


@dataclass(frozen=True)
class CompletedLink:
    link: UserLink
    dm_room_id: str


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _state_aad(state: str) -> str:
    return f"{state}\x00pkce_verifier_enc"


class DatadogOAuth:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        db: Database,
        box: SecretBox,
        site: Site,
        redirect_uri: str,
        client_name: str,
        log: Any,
        clock: Callable[[], float] = time.time,
        *,
        _test_origin: str | None = None,
    ) -> None:
        self._http = http
        self._db = db
        self._repo = LinkRepository(db, box, clock)
        self._box = box
        self._site = site
        self._redirect_uri = redirect_uri
        self._client_name = client_name
        self._log = log
        self._clock = clock
        # _test_origin is for tests only (plain-http fake server); production never sets it.
        self._test_origin = _test_origin.rstrip("/") if _test_origin else None
        self._mcp_origin = self._test_origin or f"https://mcp.{site.domain}"
        self._api_url = self._test_origin or site.api_url
        self._meta: dict[str, Any] | None = None
        self._meta_at = 0.0
        self._client_id: str | None = None

    def __repr__(self) -> str:
        return f"DatadogOAuth(site={self._site.domain})"

    # ------------------------------------------------------------------ http helpers

    def _trusted(self, url: Any) -> bool:
        if not isinstance(url, str):
            return False
        u = urllib.parse.urlsplit(url)
        if self._test_origin:
            return url.startswith(self._test_origin + "/") or url == self._test_origin
        host = u.hostname or ""
        d = self._site.domain
        return u.scheme == "https" and (host == d or host.endswith("." + d)) and not u.username

    async def _request(
        self, method: str, url: str, *, expect: tuple[int, ...] = (200,), **kw: Any
    ) -> tuple[int, Any]:
        """Returns (status, parsed JSON or None). Raises UpstreamError on transport failure."""
        try:
            async with self._http.request(
                method, url, allow_redirects=False, timeout=_TIMEOUT, **kw
            ) as resp:
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_BODY:
                        raise UpstreamError("Datadog response too large")
                status = resp.status
        except UpstreamError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise UpstreamError("Could not reach Datadog") from None
        try:
            data = json.loads(bytes(buf)) if buf else None
        except ValueError:
            data = None
        return status, data

    @staticmethod
    def _err_code(data: Any) -> str | None:
        if isinstance(data, dict):
            code = data.get("error")
            if isinstance(code, str) and _ERROR_CODE.match(code):
                return code
        return None

    # ------------------------------------------------------------------ discovery

    async def _metadata(self) -> dict[str, Any]:
        now = self._clock()
        if self._meta is not None and now - self._meta_at < METADATA_TTL_S:
            return self._meta
        prm_url = f"{self._mcp_origin}/.well-known/oauth-protected-resource/v1/mcp"
        status, prm = await self._request("GET", prm_url)
        if status != 200 or not isinstance(prm, dict):
            raise UpstreamError("Datadog OAuth discovery failed")
        servers = prm.get("authorization_servers")
        issuer = servers[0] if isinstance(servers, list) and servers else None
        if not self._trusted(issuer):
            raise UpstreamError("Datadog OAuth discovery returned an untrusted issuer")
        u = urllib.parse.urlsplit(issuer)
        base = f"{u.scheme}://{u.netloc}"
        path = u.path.rstrip("/")
        meta: dict[str, Any] | None = None
        for cand in (
            f"{base}/.well-known/oauth-authorization-server{path}",
            f"{base}/.well-known/oauth-authorization-server",
        ):
            status, data = await self._request("GET", cand)
            if status == 200 and isinstance(data, dict):
                meta = data
                break
        if meta is None:
            raise UpstreamError("Datadog OAuth discovery failed")
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            if not self._trusted(meta.get(key)):
                raise UpstreamError("Datadog OAuth metadata is incomplete or untrusted")
        methods = meta.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise UpstreamError("Datadog OAuth server does not support PKCE S256")
        self._meta, self._meta_at = meta, now
        return meta

    @staticmethod
    def _scope(meta: dict[str, Any]) -> str:
        scopes = meta.get("scopes_supported")
        if isinstance(scopes, list):
            ok = [s for s in scopes if isinstance(s, str) and s and " " not in s]
            if ok:
                return " ".join(ok)
        return "mcp_all"

    # ------------------------------------------------------------------ OAuthManager

    async def ensure_client(self) -> str:
        meta = await self._metadata()
        body = {
            "client_name": self._client_name,
            "redirect_uris": [self._redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": self._scope(meta),
        }
        status, data = await self._request("POST", meta["registration_endpoint"], json=body)
        cid = data.get("client_id") if isinstance(data, dict) else None
        if status not in (200, 201) or not isinstance(cid, str) or not cid:
            self._log.warning("Datadog client registration failed (HTTP %s)", status)
            raise UpstreamError("Datadog client registration failed")
        self._client_id = cid
        return cid

    async def _purge(self) -> None:
        await self._db.execute(
            "DELETE FROM oauth_state WHERE created_at < $1", int(self._clock()) - STATE_TTL_S
        )

    async def begin(self, mxid: str, *, dm_room_id: str) -> str:
        meta = await self._metadata()
        client_id = await self.ensure_client()
        await self._purge()
        # Only the most recent sign-in link per user stays valid.
        await self._db.execute("DELETE FROM oauth_state WHERE mxid=$1", mxid)
        state = secrets.token_urlsafe(32)
        verifier = _b64url(secrets.token_bytes(48))
        await self._db.execute(
            """INSERT INTO oauth_state (state, mxid, dm_room_id, pkce_verifier_enc, created_at)
               VALUES ($1, $2, $3, $4, $5)""",
            state,
            mxid,
            dm_room_id,
            self._box.encrypt(verifier, _state_aad(state)),
            int(self._clock()),
        )
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scope(meta),
                "state": state,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "resource": self._site.mcp_url,
            }
        )
        endpoint = self._authorize_endpoint(meta["authorization_endpoint"])
        return endpoint + ("&" if "?" in endpoint else "?") + query

    def _authorize_endpoint(self, endpoint: str) -> str:
        """Orgs on a custom subdomain sign in at ``<subdomain>.<site>``; the published metadata
        always names ``app.<site>``. Only the browser-facing authorize URL is rewritten — token
        exchange keeps using the metadata endpoint."""
        if not self._site.subdomain or self._test_origin:
            return endpoint
        u = urllib.parse.urlsplit(endpoint)
        if u.hostname != f"app.{self._site.domain}":
            return endpoint
        return urllib.parse.urlunsplit(
            (u.scheme, f"{self._site.subdomain}.{self._site.domain}", u.path, u.query, "")
        )

    async def _consume_state(
        self, state: str, mxid: str | None = None, dm_room_id: str | None = None
    ) -> Any:
        if not isinstance(state, str) or not 1 <= len(state) <= 256:
            raise ValueError("Invalid or expired sign-in link")
        row = await self._db.fetchrow(
            "SELECT mxid, dm_room_id, pkce_verifier_enc, created_at FROM oauth_state "
            "WHERE state=$1",
            state,
        )
        if row is None:
            raise ValueError("Invalid or expired sign-in link")
        # Paste-back: only the user who started sign-in, in the DM it was started from, may
        # finish it. Checked before consuming so a stray paste can't burn someone's attempt.
        if (mxid is not None and row["mxid"] != mxid) or (
            dm_room_id is not None and row["dm_room_id"] != dm_room_id
        ):
            raise ValueError("Invalid or expired sign-in link")
        # Atomic single-use: only the caller whose DELETE affects the row may proceed.
        res = await self._db.execute("DELETE FROM oauth_state WHERE state=$1", state)
        if affected_rows(res) != 1:
            raise ValueError("Invalid or expired sign-in link")
        return row

    async def complete(
        self, state: str, code: str, *, mxid: str | None = None, dm_room_id: str | None = None
    ) -> CompletedLink:
        """Finish sign-in. ``mxid``/``dm_room_id`` bind a pasted-back redirect to its requester;
        the browser-callback route leaves them unset (the state alone is the binding)."""
        row = await self._consume_state(state, mxid, dm_room_id)
        await self._purge()
        if self._clock() - row["created_at"] > STATE_TTL_S:
            raise ValueError("Invalid or expired sign-in link")
        if not isinstance(code, str) or not code:
            raise ValueError("Missing authorization code")
        mxid, dm_room_id = row["mxid"], row["dm_room_id"]
        try:
            verifier = self._box.decrypt(row["pkce_verifier_enc"], _state_aad(state))
        except ValueError:
            raise ValueError("Invalid or expired sign-in link") from None

        meta = await self._metadata()
        client_id = self._client_id or await self.ensure_client()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "code_verifier": verifier,
            "redirect_uri": self._redirect_uri,
            "resource": self._site.mcp_url,
        }
        tokens = await self._token_request(meta["token_endpoint"], form)
        handle = await self._fetch_handle(tokens.access_token)
        link = await self._repo.save_oauth(mxid, self._site.domain, tokens, handle)
        return CompletedLink(link, dm_room_id)

    async def cancel(self, state: str, *, mxid: str, dm_room_id: str) -> bool:
        """Discard a pending sign-in (e.g. the user denied consent). Same binding as complete."""
        try:
            await self._consume_state(state, mxid, dm_room_id)
        except ValueError:
            return False
        return True

    async def refresh(self, refresh_token: str) -> TokenSet:
        meta = await self._metadata()
        client_id = self._client_id or await self.ensure_client()
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": self._site.mcp_url,
        }
        return await self._token_request(meta["token_endpoint"], form, refreshing=True)

    async def _token_request(
        self, endpoint: str, form: dict[str, str], *, refreshing: bool = False
    ) -> TokenSet:
        status, data = await self._request("POST", endpoint, data=form)
        code = self._err_code(data)
        if status != 200:
            self._log.warning(
                "Datadog token endpoint returned HTTP %s (error=%s)", status, code or "-"
            )
            if refreshing and (status == 401 or (status == 400 and code in _REVOKED_CODES)):
                raise InvalidGrant
            raise UpstreamError(f"Datadog token request failed (HTTP {status})")
        if not isinstance(data, dict):
            raise UpstreamError("Datadog token response was not valid")
        access = data.get("access_token")
        if (
            not isinstance(access, str)
            or not access
            or str(data.get("token_type", "bearer")).lower() != "bearer"
        ):
            raise UpstreamError("Datadog token response was not valid")
        refresh = data.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            refresh = None
        ttl = data.get("expires_in")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            ttl = 3600
        return TokenSet(access, refresh, int(self._clock() + min(ttl, 86400)))

    async def _fetch_handle(self, access_token: str) -> str | None:
        """Best-effort identity lookup (handle/email only). Never raises."""
        try:
            status, data = await self._request(
                "GET",
                f"{self._api_url}/api/v2/current_user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except UpstreamError:
            return None
        if status != 200 or not isinstance(data, dict):
            return None
        d = data.get("data")
        attrs = d.get("attributes") if isinstance(d, dict) else None
        if not isinstance(attrs, dict):
            return None
        for key in ("handle", "email"):
            val = attrs.get(key)
            if isinstance(val, str) and val.strip():
                return "".join(c for c in val.strip()[:200] if c.isprintable())
        return None

    def revoke_instructions(self, site: Site) -> str:
        return (
            "Logging out removes the stored credentials from this bot. To also revoke the "
            f"bot's access in Datadog, open your Datadog account settings ({site.app_url}) and "
            "remove the authorized application or OAuth access for this bot "
            f"({self._client_name}) in your personal settings."
        )
