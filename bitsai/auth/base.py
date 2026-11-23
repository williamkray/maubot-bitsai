"""Credential contracts.

Model: **one OAuth client registration per plugin instance** (dynamic client registration
against the Datadog MCP authorization server), **one token set per Matrix user**. A user's
tokens only ever act as that user's Datadog identity.

Security invariants (reviewed by Opus — do not weaken without review):
- Secrets are encrypted at rest (AES-256-GCM, key from config ``security.token_encryption_key``)
  and never logged, rendered, or included in exceptions.
- OAuth ``state`` is random (>=128 bits), single-use, expires after 10 minutes, and is bound to
  the mxid that requested it. PKCE S256 is always used.
- Refresh is serialised per user (one in-flight refresh per mxid) because refresh tokens may
  rotate; a failed refresh deletes the link and raises AuthRequired.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol


class CredentialKind(enum.Enum):
    OAUTH = "oauth"  # access + refresh token from the MCP authorization server (scope mcp_all)
    PAT = "pat"  # Datadog Personal Access Token pasted via DM


@dataclass(frozen=True)
class Credential:
    kind: CredentialKind
    access_token: str
    expires_at: float | None = None  # unix seconds; None = no known expiry (PAT)

    def __repr__(self) -> str:  # never leak tokens via logs/tracebacks
        return f"Credential(kind={self.kind.value}, expires_at={self.expires_at})"


@dataclass(frozen=True)
class UserLink:
    mxid: str
    site: str
    kind: CredentialKind
    dd_user_handle: str | None
    linked_at: float


class TokenProvider(Protocol):
    async def get(self, mxid: str) -> Credential:
        """Valid credential for ``mxid``, refreshing if within 60 s of expiry.
        Raises ``backend.base.AuthRequired`` if none/unrefreshable."""
        ...


class OAuthManager(Protocol):
    async def ensure_client(self) -> str:
        """Register via DCR with this deployment's redirect_uri and return the client_id.
        Call before every ``begin()``: Datadog returns one shared client_id for all
        registrations, so re-asserting our redirect_uri is cheap and guards against overwrites."""
        ...

    async def begin(self, mxid: str, *, dm_room_id: str) -> str:
        """Create state + PKCE verifier, persist them, return the authorization URL.
        Callers MUST only send the URL to ``dm_room_id``, a verified 1:1 DM with ``mxid``
        (exactly 2 joined members): whoever completes consent decides the linked identity."""
        ...

    async def complete(self, state: str, code: str) -> Any:
        """Validate+consume state, exchange code, store encrypted tokens.
        Returns ``oauth.CompletedLink(link, dm_room_id)``; the caller confirms in that DM
        ("Linked as <handle>. Not you? !bits logout"). Raises ValueError on bad state."""
        ...


class LinkStore(Protocol):
    async def get_link(self, mxid: str) -> UserLink | None: ...
    async def save_pat(
        self, mxid: str, site: str, token: str, dd_user_handle: str | None
    ) -> UserLink: ...
    async def delete(self, mxid: str) -> bool: ...
