"""Detect that a message addresses the bot, and extract the question.

Handles, in order of reliability:
- ``m.mentions.user_ids`` (intentional mentions, modern clients);
- a pill link to the bot in ``formatted_body`` (``https://matrix.to/#/@bot:server``);
- plain text: the bot's MXID anywhere, or its localpart / display name at the start
  followed by ``:`` / ``,`` / whitespace (e.g. "bitsdev: what's up with checkout?").
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

_LEADING_SEP = r"[\s:,]*"


def _names(mxid: str, displayname: str | None) -> list[str]:
    localpart = mxid[1:].split(":", 1)[0]
    names = [n for n in (displayname, localpart) if n]
    return sorted(set(names), key=len, reverse=True)  # longest first


def _pill_mentions(formatted: str, mxid: str) -> bool:
    for href in re.findall(r"""href=["']([^"']+)["']""", formatted, flags=re.I):
        if urllib.parse.unquote(href).split("?", 1)[0].endswith("/#/" + mxid):
            return True
    return False


def extract_question(
    content: dict[str, Any], mxid: str, displayname: str | None = None
) -> str | None:
    """Return the message text with the bot mention removed if the bot is addressed,
    else None. ``content`` is the serialized m.room.message content."""
    body = str(content.get("body") or "")
    formatted = str(content.get("formatted_body") or "")
    mentions = content.get("m.mentions") or {}
    ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
    names = _names(mxid, displayname)

    addressed = (
        (isinstance(ids, list) and mxid in ids)
        or (bool(formatted) and _pill_mentions(formatted, mxid))
        or mxid in body
    )
    text = body.replace(mxid, " ")
    lead = re.compile(
        r"^\s*@?(" + "|".join(re.escape(n) for n in names) + r")(?=[\s:,]|$)" + _LEADING_SEP,
        re.I,
    )
    m = lead.match(text)
    if m:
        addressed = True
        text = text[m.end():]
    if not addressed:
        return None
    # Pills render as the display name in the plain body; drop a leading one if still there.
    text = lead.sub("", text, count=1)
    return " ".join(text.split())
