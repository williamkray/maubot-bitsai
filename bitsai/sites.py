"""Datadog site -> endpoint URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Sites where the remote MCP server is available (US1-FED / US2-FED are not).
SUPPORTED_SITES = (
    "datadoghq.com",
    "us3.datadoghq.com",
    "us5.datadoghq.com",
    "datadoghq.eu",
    "ap1.datadoghq.com",
    "ap2.datadoghq.com",
    "uk1.datadoghq.com",
)

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class Site:
    domain: str
    subdomain: str = ""  # custom org subdomain, used only for user-facing links

    def __post_init__(self) -> None:
        if self.domain not in SUPPORTED_SITES:
            raise ValueError(f"Unsupported Datadog site {self.domain!r}")
        if self.subdomain and not _LABEL.fullmatch(self.subdomain):
            raise ValueError(f"Invalid Datadog subdomain {self.subdomain!r}")

    @property
    def app_url(self) -> str:
        """Origin for OAuth endpoints and app links (``app.<site>``)."""
        return f"https://app.{self.domain}"

    @property
    def link_base(self) -> str:
        """Origin for user-facing deep links; honours a custom org subdomain."""
        return f"https://{self.subdomain}.{self.domain}" if self.subdomain else self.app_url

    @property
    def api_url(self) -> str:
        return f"https://api.{self.domain}"

    @property
    def mcp_url(self) -> str:
        return f"https://mcp.{self.domain}/v1/mcp"

    def absolute(self, link: str) -> str:
        """Turn Bits' relative links (``/monitors/123``) into absolute ones."""
        return self.link_base + link if link.startswith("/") else link
