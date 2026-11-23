from __future__ import annotations

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    """Typed view of base-config.yaml. Keys mirror that file one-to-one."""

    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("datadog.site")
        helper.copy("datadog.subdomain")
        helper.copy("datadog.mcp_toolsets")
        helper.copy("security.token_encryption_key")
        helper.copy("auth.allow_pat_login")
        helper.copy("auth.oauth_client_name")
        helper.copy("auth.oauth_redirect")
        helper.copy("chat.command")
        helper.copy("chat.respond_in_dms")
        helper.copy("chat.respond_to_mentions")
        helper.copy("chat.timeout_seconds")
        helper.copy("investigations.poll_max_minutes")
        helper.copy("rate_limits.per_user_per_minute")
        helper.copy("rate_limits.per_room_per_minute")
        helper.copy("unfurl.default_enabled")
        helper.copy("admins")
