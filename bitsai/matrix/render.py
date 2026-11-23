"""Pure rendering helpers: Bits Markdown / alert dicts -> Matrix notice content."""

from __future__ import annotations

import re
from html import escape
from typing import Any

from mautrix.types import Format, MessageType, TextMessageEventContent
from mautrix.util.markdown import md_parser, no_html_renderer

from ..backend.base import (
    Investigation,
    InvestigationStatus,
    PendingAction,
    ResourceSummary,
)
from ..sites import Site

MAX_BODY_BYTES = 30_000
TRUNCATION_TAIL = "…(truncated, open in Datadog)"
_SAFE_SCHEMES = ("http://", "https://", "mailto:")


def _content(body: str, html: str) -> TextMessageEventContent:
    return TextMessageEventContent(
        msgtype=MessageType.NOTICE, format=Format.HTML, body=body, formatted_body=html
    )


def _truncate(text: str, limit: int = MAX_BODY_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    cut = raw[:limit].decode("utf-8", errors="ignore").rstrip()
    return cut + "\n\n" + TRUNCATION_TAIL


def _safe_url(url: str, site: Site) -> str:
    url = url.strip()
    if url.startswith("//"):
        return ""
    url = site.absolute(url)
    return url if url.lower().startswith(_SAFE_SCHEMES) else ""


def _rewrite_links(ast: Any, site: Site) -> None:
    walker = ast.walker()
    for node, entering in walker:
        if entering and node.t in ("link", "image"):
            node.destination = _safe_url(node.destination or "", site)


def render_markdown(markdown: str, site: Site) -> tuple[str, str]:
    markdown = _truncate(markdown)
    ast = md_parser.parse(markdown)
    _rewrite_links(ast, site)
    html = no_html_renderer.render(ast).strip()
    return markdown, html


def render_chat_answer(markdown: str, site: Site) -> TextMessageEventContent:
    body, html = render_markdown(markdown or "", site)
    return _content(body, html)


def render_notice(markdown: str) -> TextMessageEventContent:
    """Bot-authored Markdown notice (commands, hints). Raw HTML is escaped, so interpolated
    values (handles, room IDs, alert titles) can't inject markup."""
    markdown = _truncate(markdown or "")
    return _content(markdown, no_html_renderer.render(md_parser.parse(markdown)).strip())


def render_thinking(for_user: str | None = None) -> TextMessageEventContent:
    text = f"Bits AI is thinking (for {for_user})…" if for_user else "Bits AI is thinking…"
    return _content(text, f"<em>{escape(text)}</em>")


def render_error(msg: str) -> TextMessageEventContent:
    return _content(f"⚠️ {msg}", f"⚠️ {escape(str(msg))}")


def render_pending_action(pa: PendingAction, site: Site) -> TextMessageEventContent:
    desc_body, desc_html = render_markdown(pa.description or "", site)
    prompt = "React ✅ to approve or ❌ to deny."
    body = f"Bits AI needs your approval:\n\n{desc_body}\n\n{prompt}"
    html = f"<p><strong>Bits AI needs your approval:</strong></p>{desc_html}<p>{prompt}</p>"
    return _content(body, html)


def _status_label(inv: Investigation) -> str:
    return inv.raw_status or inv.status.value


def _title(inv: Investigation) -> str:
    return inv.title or f"Investigation {inv.id}"


def render_investigation_status(inv: Investigation, elapsed_s: int) -> TextMessageEventContent:
    mins, secs = divmod(max(int(elapsed_s), 0), 60)
    elapsed = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
    title, status = _title(inv), _status_label(inv)
    body = f"🔎 {title}\nStatus: {status} ({elapsed})"
    html = f"🔎 <strong>{escape(title)}</strong><br>Status: {escape(status)} ({elapsed})"
    if inv.url and inv.url.startswith(_SAFE_SCHEMES):
        body += f"\n{inv.url}"
        html += f'<br><a href="{escape(inv.url, quote=True)}">Open in Datadog</a>'
    return _content(body, html)


def render_investigation_result(
    inv: Investigation, site: Site, detailed: bool = False
) -> TextMessageEventContent:
    title, status = _title(inv), _status_label(inv)
    done = inv.status is InvestigationStatus.CONCLUSIVE
    icon = "✅" if done else "⚠️"
    body_parts = [f"{icon} {title}", f"Status: {status}"]
    html_parts = [f"<p>{icon} <strong>{escape(title)}</strong><br>Status: {escape(status)}</p>"]
    for c in inv.conclusions:
        body_parts.append(f"{c.title}\n{c.summary}")
        html_parts.append(f"<p><strong>{escape(c.title)}</strong><br>{escape(c.summary)}</p>")
        if detailed and c.description:
            d_body, d_html = render_markdown(c.description, site)
            body_parts.append(d_body)
            html_parts.append(d_html)
    if inv.url and inv.url.startswith(_SAFE_SCHEMES):
        body_parts.append(f"Open in Datadog: {inv.url}")
        html_parts.append(f'<p><a href="{escape(inv.url, quote=True)}">Open in Datadog</a></p>')
    body, html = "\n\n".join(body_parts), "".join(html_parts)
    if len(body.encode()) > MAX_BODY_BYTES:
        body = _truncate(body)
        html = _truncate(html) + "</p>"  # best effort; clients tolerate unbalanced tags
    return _content(body, html)


def render_resource_card(r: ResourceSummary) -> TextMessageEventContent:
    head = f"{r.kind.value.capitalize()}: {r.title}"
    body_lines = [head]
    link = f'<a href="{escape(r.url, quote=True)}">{escape(r.title)}</a>'
    html = f"<strong>{escape(r.kind.value.capitalize())}</strong>: {link}"
    if r.status:
        body_lines.append(f"Status: {r.status}")
        html += f"<br>Status: {escape(r.status)}"
    for k, v in r.fields:
        body_lines.append(f"{k}: {v}")
        html += f"<br>{escape(k)}: {escape(v)}"
    body_lines.append(r.url)
    return _content("\n".join(body_lines), html)


_STATUS_ICONS = {"alert": "🔴", "warn": "🟠", "warning": "🟠", "ok": "🟢", "recovered": "🟢"}


def render_alert(payload: dict, site: Site) -> TextMessageEventContent:
    def s(key: str) -> str:
        v = payload.get(key)
        return "" if v is None else str(v)

    title = s("title") or "Datadog alert"
    status = s("status")
    transition = s("transition")
    icon = _STATUS_ICONS.get(re.sub(r"[^a-z]", "", status.lower()), "🔔")
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = [str(t) for t in tags]
    link = site.absolute(s("link"))
    if not link.lower().startswith(_SAFE_SCHEMES):
        link = ""
    snapshot = s("snapshot_url")
    if not snapshot.lower().startswith("https://"):
        snapshot = ""

    state = " — ".join(x for x in (status, transition) if x)
    body = [f"{icon} {title}"]
    html = [f"{icon} <strong>{escape(title)}</strong>"]
    if state:
        body.append(state)
        html.append(escape(state))
    if tags:
        body.append("Tags: " + ", ".join(tags))
        html.append("Tags: " + escape(", ".join(tags)))
    if snapshot:
        body.append(f"Snapshot: {snapshot}")
        html.append(f'<a href="{escape(snapshot, quote=True)}">Snapshot</a>')
    if link:
        body.append(f"Open in Datadog: {link}")
        html.append(f'<a href="{escape(link, quote=True)}">Open in Datadog</a>')
    return _content("\n".join(body), "<br>".join(html))
