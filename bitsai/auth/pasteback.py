"""Loopback redirect + paste-back for OAuth sign-in.

Datadog's /oauth2/v1/authorize only accepts loopback redirect URIs for the shared DCR client
(docs/FINDINGS.md §5), so a maubot on a public https URL can't receive the callback. Instead we
redirect to a loopback URL nothing serves: the browser shows a connection error, and the user
pastes the address-bar URL (carrying ``code`` and ``state``) back to the bot in their DM.

The code is single-use and useless without the PKCE verifier, which never leaves the bot.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

# Fixed and loopback-only. Port is arbitrary (Datadog accepts any loopback port/path); nothing is
# expected to listen there. If something does, it only ever sees a verifier-bound code.
PASTE_REDIRECT_URI = "http://localhost:8765/maubot-bitsai/callback"

_URL = re.compile(re.escape(PASTE_REDIRECT_URI) + r"\?[^\s<>\"'`]+")
_ERROR = re.compile(r"^[a-z_]{1,40}$")


@dataclass(frozen=True)
class PastedRedirect:
    state: str = field(repr=False)
    code: str = field(repr=False)  # "" when Datadog returned an error instead
    error: str | None = None

    def __repr__(self) -> str:  # never leak code/state
        return f"PastedRedirect(error={self.error!r})"


def contains_redirect(text: str) -> bool:
    return PASTE_REDIRECT_URI in (text or "")


def parse_redirect(text: str) -> PastedRedirect | None:
    """Extract our redirect from a pasted message; None if absent or malformed."""
    m = _URL.search(text or "")
    if not m:
        return None
    query = m.group(0).split("?", 1)[1].rstrip(".,;:)]}>")
    qs = urllib.parse.parse_qs(query, keep_blank_values=True)
    states = qs.get("state", [])
    if len(states) != 1 or not states[0]:
        return None
    codes = qs.get("code", [])
    errors = qs.get("error", [])
    if errors:
        err = errors[0] if _ERROR.match(errors[0] or "") else "error"
        return PastedRedirect(states[0], "", err)
    if len(codes) != 1 or not codes[0]:
        return None
    return PastedRedirect(states[0], codes[0])
