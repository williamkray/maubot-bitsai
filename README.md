# maubot-bitsai

A [maubot](https://github.com/maubot/maubot) plugin that brings Datadog Bits AI to Matrix:
chat, Bits AI SRE investigations, monitor alerts and link previews. Each user signs in with
their own Datadog account, and Bits only sees what that user can see.

## Setup

1. Download the `.mbp` from [Releases](https://github.com/williamkray/maubot-bitsai/releases)
   (or build it with `mbc build`) and upload it in the maubot admin UI.
2. Create an instance (it needs a database).
3. Set `security.token_encryption_key` in the instance config. It encrypts stored Datadog tokens:

   ```sh
   python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
   ```

4. Set `datadog.site` (and `datadog.subdomain` if your org uses one), then save.
5. In Datadog, **MCP Access** must be on (Organization Settings > MCP Server), and users need the
   MCP Read and MCP Write permissions (the Datadog Standard Role has both).

### Sign-in mode

By default (`auto`), Datadog redirects straight back to this maubot after sign-in. A Datadog org
admin must add this URL under Organization Settings > MCP Server > **MCP OAuth Redirect URLs**:

```
<maubot public_url>/_matrix/maubot/plugin/<instance id>/oauth/callback
```

The plugin builds this URL from maubot's `server.public_url`, so that must be your public https
address (not the default `http://localhost:29316`). The plugin logs the redirect URL it uses at
startup. If it isn't allow-listed, sign-in fails with "Mismatching redirect URI".

If you can't get the URL allow-listed, set `auth.oauth_redirect: paste`. Datadog then redirects
to a `localhost` page (always allowed), and the user pastes that page's address to the bot.

## Configuration

| Key | Default | Purpose |
|---|---|---|
| `datadog.site` | `datadoghq.com` | Your Datadog site (`datadoghq.eu`, `us5.datadoghq.com`, …) |
| `datadog.subdomain` | `""` | Custom org subdomain, used for sign-in and links |
| `security.token_encryption_key` | — | **Required.** 32 random bytes, base64-encoded |
| `auth.allow_pat_login` | `true` | Allow sign-in with a Personal Access Token |
| `auth.oauth_redirect` | `auto` | `auto`/`callback`: redirect to this maubot; `paste`: paste-back (see [Sign-in mode](#sign-in-mode)) |
| `chat.command` | `bits` | Command prefix |
| `chat.respond_to_mentions` | `true` | Answer when the bot is mentioned |
| `chat.respond_in_dms` | `false` | Answer every DM message (leave off if other plugins share the bot account) |
| `chat.timeout_seconds` | `180` | How long to wait for a Bits answer |
| `investigations.poll_max_minutes` | `30` | How long to follow an investigation |
| `rate_limits.*` | `6` / `20` | Requests per minute, per user / per room |
| `admins` | `[]` | Matrix IDs that can run room-admin commands anywhere |

## Use

**Sign in** (in a DM with the bot): `!bits login`. Open the link and approve access. In paste-back
mode your browser then fails to load a `localhost` page; that's expected. Paste that page's full
address back to the bot. Or use `!bits login token <PAT>`.

| Command | |
|---|---|
| `!bits ask <question>` or mention the bot | Ask Bits; reply in the thread to follow up |
| `!bits investigate <description>` | Start an investigation |
| `!bits steer <message>` | Steer the investigation in this thread |
| `!bits whoami` / `!bits logout` | Show or remove your linked account |
| `!bits webhook` / `!bits webhook revoke` | Room admins: create or revoke the alert webhook (setup sent by DM) |
| `!bits unfurl on\|off` | Room admins: Datadog link previews |
| `!bits context on\|off` | Room admins: give people who join a thread its earlier messages as context |

**Reactions:** 🔍 on an alert starts an investigation, 💬 asks Bits about it. ✅ / ❌ approve or
deny an action Bits wants to take.

## License

MIT
