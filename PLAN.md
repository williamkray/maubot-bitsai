# maubot-bitsai — Design & Implementation Plan

Bring the Bits AI experience available in Slack and Microsoft Teams to Matrix as an open-source maubot plugin.

## Decisions (locked)

| Topic | Decision |
|---|---|
| Brain | **Bits-native only.** The plugin uses Bits; it does not host its own LLM. |
| Auth | **Per-user OAuth.** Each Matrix user links their own Datadog identity, and Datadog RBAC is respected. |
| Audience | **Open source for any Datadog customer.** Every site is configurable and documented. |
| v1 scope | Threaded chat Q&A, investigations, monitor/incident push, rich previews and reaction actions. |

## What the research established (2026-11-12)

### Datadog

- **There is no public REST API for Bits Chat.** The only programmatic route is the remote MCP server's `chat` toolset:
  - `send_message_to_chat`
  - `get_chat_message_result`
  - `get_chat_conversation_history`
  - `list_chat_conversations`
- The chat toolset looks asynchronous (send, then poll). This is **unverified**, and so is whether it is GA.
- **Remote MCP server:**
  - URL is `https://mcp.<site>/v1/mcp`, over Streamable HTTP.
  - Toolsets are selected with `?toolsets=core,chat,investigator,...`.
  - Auth can be OAuth, PAT/SAT bearer tokens, or API key + app key.
  - US1-FED and US2-FED are not supported.
- **Investigations** are available two ways:
  - REST: `POST/GET /api/v2/bits-ai/investigations[/{id}]`. The API looks like preview: unstable-operations flag, undocumented status values, no completion webhook.
  - MCP `investigator` toolset, which also has `steer_bits_ai_investigation`.
- **Webhooks** are GA. Payloads can be customised and custom headers set. Delivery has a 15 s timeout and retries only on 5xx. `$SNAPSHOT` gives a pre-rendered graph image URL.
- **OAuth:**
  - Datadog's documented third-party OAuth targets Marketplace partners.
  - It is undocumented whether a self-hosted open-source tool can register without a Marketplace listing.
  - The **MCP server's own OAuth flow** (the MCP-spec discovery + dynamic client registration that Claude Code and Cursor use) is the likelier route.
- **Snapshot API** (`/api/v1/graph/snapshot`) still exists. Monitor, dashboard, incident and notebook GET APIs exist for unfurls.

### maubot

- `webapp: true` provides routes at `<public_url>/_matrix/maubot/plugin/<instance_id>/...`. These are used for the OAuth callback and webhooks.
- Web routes are **unauthenticated**, so the plugin must verify OAuth state and webhook secrets itself.
- `database_type: asyncpg` must be set explicitly. Use `UpgradeTable` migrations. SQLite is the default; Postgres is optional.
- Message helpers:
  - `evt.respond(..., in_thread=True)` for threads.
  - `evt.respond(..., edits=event_id)` for progressive "streaming" edits. These are not throttled, so the plugin must throttle them.
  - `EventType.REACTION` for reaction actions.
- Encrypted rooms are handled transparently at the client level.
- **maubot does not pip-install plugin dependencies.** We should stick to what ships with maubot (aiohttp, yarl, attrs, mautrix, ruamel.yaml, jinja2, markdown) plus vendored pure-Python code.
  - So we do **not** use the official `mcp` SDK, which pulls in pydantic, httpx and anyio. We write a small MCP Streamable-HTTP JSON-RPC client on aiohttp instead.
- The reference implementation to follow is `maubot/github`, which combines OAuth login, webhooks, the asyncpg DB and config.

## Phase 0 results (verified 2026-11-12 against a US1 sandbox org)

Evidence is in `docs/FINDINGS.md`. Redacted fixtures are in `tests/fixtures/`.

### MCP transport

| Finding | Design impact |
|---|---|
| A PAT sent as a Bearer token works. Protocol version is `2025-06-18`, and the server issues an `Mcp-Session-Id`. | The spike's prototype client became the basis of `bitsai/mcp/client.py`. |
| `initialize` and `tools/list` return JSON, but every `tools/call` returns **SSE**. Results are a JSON string in `content[0].text` and need a second parse. | The client must handle both, plus the double parse. |
| Every tool requires a `telemetry: {intent}` argument. | The backend injects it automatically. |
| The `chat` toolset is **not** included in `toolsets=all` and must be requested explicitly. | Use `toolsets=core,chat,investigator`. |

### Chat

| Finding | Design impact |
|---|---|
| **Asynchronous with long polling.** `send_message_to_chat` returns `queued` + `task_id` + `conversation_id` in about 0.2 s. `get_chat_message_result` then blocks on the server for about 4–16 s and returned `completed` on the first poll. | Loop until a final state, with an overall timeout. |
| **No partial text.** | Progressive edits of the answer aren't possible. Post a "Thinking…" placeholder (with a typing indicator), then edit it once with the final answer. Showing intermediate steps from `get_chat_conversation_history` while a request runs is a later option and unverified. |
| Reusing the same `conversation_id` continues the conversation, which maps cleanly to a Matrix thread. | Store the mapping per thread. |
| Answers are Markdown with **relative links** (`/monitors/<id>`). | The renderer must prefix the org's app URL. |
| Latency was about 7–19 s. | Acceptable for a placeholder-then-answer flow. |
| The docs also describe `requires_action` (with `pending_action`), `failed` and `cancelled`, but none were observed. | `requires_action` will probably map to ✅/❌ reactions for confirmation. Not tested. |

### Investigations

| Finding | Design impact |
|---|---|
| REST `POST/GET /api/v2/bits-ai/investigations` accepts a PAT as Bearer and needs no unstable-operations flag. | Works for the PAT path. |
| Observed statuses were `in progress` (with a space) and then `conclusive`, about 3 min apart. The MCP docs also mention `failed`, `needs direction` and `inconclusive`. | Treat any status not in a known-terminal set as still running. |
| The MCP `get_bits_ai_investigation` response is about 60 KB, much richer than REST (evidence, hypotheses, steps, remediations). | Summarise it; the full detail can go into a thread reply on demand. |

### Permissions

| Finding | Design impact |
|---|---|
| The relevant permissions are `assistant_access`, `mcp_read`, `mcp_write`, `bits_investigations_read` and `bits_investigations_write`. | Basis for the documented permission list. |
| Chat worked even though "MCP Write Access" is disabled for the org. | Unclear whether triggering an investigation **over MCP** needs `mcp_write`. Not tested. |
| Minimum scopes are inferred from documentation only. No reduced-scope token was tested. | Still to verify. |

### OAuth via the MCP server

| Finding | Design impact |
|---|---|
| Discovery works without authentication. Endpoints are on `app.datadoghq.com`, PKCE S256 is required, the only scope is `mcp_all`, and the client is public (auth method `none`). | Per-user OAuth needs no Marketplace listing or app key. |
| Dynamic client registration is anonymous and **accepted a non-localhost callback URL**. | A self-hosted plugin can register itself. |
| Registration appears to be an **upsert keyed on `client_name`**. | Use a unique `client_name` per deployment, e.g. one derived from the public URL and instance ID. |

### Snapshots and getters

| Finding | Design impact |
|---|---|
| `/api/v1/graph/snapshot` works with a PAT and returns a public PNG URL. | Usable for previews and alerts. |
| REST and MCP getters for monitors, dashboards and notebooks work. The incident getter is unverified because the org has no incidents. | Unfurls can use either path. |

### Phase 0b results: OAuth completed end to end (2026-11-12)

| Finding | Design impact |
|---|---|
| Consent → code → token exchange works with PKCE + RFC 8707 `resource=https://mcp.<site>/v1/mcp`. | Implement exactly this flow. |
| The access token is a JWT (PS256) that lasts **3600 s**. | Refresh shortly before expiry. |
| The `scope` the token actually carries is **every permission the user has**, not a narrow `mcp_all`. | A stored token is as powerful as the user: for an admin, that includes `api_keys_write`. Encryption at rest and never logging are essential. Document this. |
| **The OAuth token works on REST too:** `current_user` and `bits-ai/investigations` both returned 200. | `RestBackend` works for OAuth users, not only on the PAT path. |
| The refresh token is **not rotated**, and the old one stays reusable. It is a long-lived bearer secret. | Store it encrypted. `!bits logout` must delete it locally and tell the user how to revoke the app in Datadog. A per-user refresh lock is kept anyway, as defence in case rotation is turned on later. |
| **DCR returns the same `client_id` (`51ea5567…`) whatever `client_name` or callback URL is sent.** | Datadog appears to use one shared public client for MCP. The earlier theory that registrations are upserted by `client_name` is wrong. There's no need to persist a client_id. |
| An unauthenticated probe can't tell whether callback URLs are validated against a registration, because the authorize endpoint redirects to login first. | Re-run DCR with this deployment's callback URL right before every `begin()`. It's cheap and idempotent, and harmless if registrations get overwritten. Confirm with a real deployment URL in Phase 4. |

### Still unverified

1. Whether an MCP-triggered investigation needs `mcp_write`. This needs a write, so I'll ask you first.
2. Whether redirect URIs are enforced after login. To be checked in Phase 4 using a real public deployment URL.

### Resulting design changes

- **The backend is MCP-first.** Chat, investigations (trigger, get and steer) and unfurl getters all go through MCP. `RestBackend` becomes an optional adapter for the PAT path and for snapshots.
- **Chat UX:** a placeholder, then one final edit; `requires_action` maps to ✅/❌ reactions. Progressive "streaming" applies to investigation status only.
- **Login options:** the primary path is `!bits login`, using OAuth with dynamic client registration and PKCE. The fallback is `!bits login token`, which accepts a PAT.

## Architecture

```
Matrix room/DM ──► BitsPlugin (commands, events, reactions)
                     │
       ┌─────────────┼──────────────────┬───────────────┐
   ChatService  InvestigationService  AlertRouter     Unfurler
       │             │                 ▲ (webhooks)     │
       └──────┬──────┴─────────┬───────┘                │
          BitsBackend (Protocol)   ◄────────────────────┘
          ├── McpBackend  (chat, investigator, core tools)
          └── RestBackend (investigations, monitors, incidents, snapshots)
                 │
             McpClient (aiohttp, Streamable HTTP, JSON-RPC)
                 │
             TokenProvider ── OAuthManager (discovery, DCR, PKCE, refresh)
                 │                 └── TokenStore (encrypted at rest, DB)
             SiteRegistry (US1/US3/US5/EU1/AP1/AP2/UK1 → app/api/mcp URLs)
```

**Key design choice:** every feature talks to the `BitsBackend` Protocol, not to MCP or REST directly. The preview APIs will change, and when they do only one adapter needs fixing. Tests run against recorded fixtures.

### Module layout

```
maubot.yaml  base-config.yaml  LICENSE  README.md
bitsai/
  __init__.py      plugin.py        # BitsPlugin: wiring, commands, event handlers
  config.py        sites.py         # BaseProxyConfig; site → URL map
  db.py                             # UpgradeTable + small repositories
  mcp/client.py                     # JSON-RPC over Streamable HTTP, session id, SSE parse
  auth/oauth.py    auth/tokens.py   # MCP OAuth (RFC 9728/8414/7591 + PKCE), encrypted store
  backend/base.py  backend/mcp.py  backend/rest.py
  services/chat.py services/investigations.py services/alerts.py services/unfurl.py
  matrix/render.py matrix/throttle.py matrix/reactions.py
  web.py                            # /oauth/callback, /hook/<room_token>
tests/ (pytest, fixtures/ recorded MCP + REST responses)
```

### Data model (v1)

| Table | Columns |
|---|---|
| `user_link` | `mxid` PK, `site`, `dd_user_handle`, `access_token_enc`, `refresh_token_enc`, `expires_at`, `client_id` |
| `oauth_state` | `state` PK, `mxid`, `pkce_verifier`, `created_at` (TTL 10 min) |
| `conversation` | `room_id`, `thread_root` PK, `bits_conversation_id`, `owner_mxid` |
| `investigation` | `investigation_id` PK, `room_id`, `thread_root`, `status_event_id`, `requested_by`, `last_status` |
| `room_settings` | `room_id` PK, `site`, `webhook_token_hash`, `unfurl_enabled`, `alert_format` |
| `alert_message` | `event_id` PK, `room_id`, `monitor_id`, `alert_event_id`, `event_ts` (maps reactions back to alerts) |

### Feature behaviour

1. **Linking accounts**
   - `!bits login` (DM only) sends a one-time authorisation link with PKCE and state.
   - The callback stores tokens encrypted with a key from config. Refresh tokens are used automatically.
   - Fallback: `!bits login token`, which accepts a PAT/SAT in a DM and redacts the message afterwards. This covers sites or orgs where OAuth client registration doesn't work.
   - Also: `!bits logout` and `!bits whoami`.
2. **Chat**
   - Trigger: `!bits ask …` or a mention. Also any message in a DM if `chat.respond_in_dms` is on (default off: other plugins on the same bot account share its DMs).
   - The bot opens or continues a Matrix thread, which maps 1:1 to a Bits conversation ID.
   - It posts "Thinking…", polls `get_chat_message_result`, and edits the message as results arrive (throttled to 1 edit per ~1.5 s, final edit unthrottled).
   - It always uses the **asking user's** token. Unlinked users get a DM nudge.
3. **Investigations**
   - Start with `!bits investigate <description>`, or react 🔍 on an alert message.
   - A background poller edits a status message in the thread, then posts the conclusions.
   - `!bits steer <text>` inside the thread uses the MCP steer tool.
   - Polling uses backoff, and any investigations still running are resumed after a restart (they are stored in the DB).
4. **Alerts**
   - `!bits webhook` (room admins only) creates a per-room secret token and prints the URL, a JSON payload template and header config to paste into Datadog's Webhooks tile.
   - On an inbound alert:
     - The secret is checked with a constant-time compare.
     - The bot returns 200 quickly and processes the alert asynchronously, to stay inside Datadog's 15 s timeout.
     - Events are deduplicated by alert ID and transition.
     - The bot posts a formatted alert using the `$SNAPSHOT` image.
   - Reactions on the alert: 🔍 investigate, 💬 ask Bits about it in a thread.
5. **Unfurls**
   - Detects `app.<site>` links to monitors, dashboards, incidents and notebooks, and posts a compact card.
   - **Off by default per room**, and uses the poster's token: an unfurl could reveal data to room members who lack access in Datadog. This has to be documented prominently.

### Security requirements (Opus reviews these)

- Tokens are encrypted at rest. The key comes from config, and a missing key is a fatal start-up error. Tokens are never logged.
- OAuth `state` is single-use with a TTL and bound to the mxid. PKCE S256 is always used.
- **Sign-in links go only to a real 1:1 DM.** Binding `state` to the mxid doesn't stop a link being *used* by someone else: whoever approves in Datadog decides which Datadog identity gets attached to the requester's Matrix account. So:
  - `!bits login` in a shared room replies "sent you a DM" and opens one.
  - A room counts as a DM only if it has exactly 2 joined members (the bot and the requester), checked again when the link is sent.
  - Links are single-use with a 10-minute TTL.
  - After the callback, the bot DMs "Linked as <Datadog user>. Not you? `!bits logout`", so a mismatch is visible straight away.
- The webhook token is stored hashed. Inbound payloads have a size limit and are schema-validated.
- Commands that handle credentials are DM-only. A secret pasted into a room is redacted, and the user is warned.
- Room-level settings (webhooks, unfurls) are limited to room admins (power level ≥ 50).
- Rate limits are enforced per user and per room, to protect the Datadog org's Bits quota.

## Phases

The goal is to keep Opus on design and security and push the volume work to cheaper models.

| # | Phase | Work | Model | Exit criteria |
|---|---|---|---|---|
| **0** | **Verification spike (gating)** | Standalone script, with your test-org credentials, that does three things. (1) Run MCP `initialize` + `tools/list` with `toolsets=core,chat,investigator` and save the tool schemas. (2) Run one chat round trip and one investigation round trip, and save the raw responses as fixtures. (3) Try MCP OAuth discovery + dynamic client registration with a non-localhost redirect URI. | Sonnet (you supply credentials) | Chat semantics confirmed, fixtures captured, OAuth path chosen (MCP DCR vs Developer Platform vs PAT-only). |
| 0b | OAuth completion | Local callback on localhost. You approve once in a browser. Then: exchange the code for tokens, refresh once, call one MCP tool and one REST endpoint with the OAuth token, and try an MCP investigation trigger only if you approve it. | Sonnet + you | Token lifetime and refresh behaviour known; REST yes/no known |
| 1 | Contracts & scaffold | `BitsBackend` Protocol, service interfaces with docstrings, DB schema, config schema, `CLAUDE.md` conventions | **Opus** | Interfaces frozen |
| 1b | | `maubot.yaml`, `base-config.yaml`, pytest setup, CI (lint + test + `mbc build`), Docker compose (Synapse + maubot) | Haiku | CI green on stubs |
| 2 | Core infrastructure (parallel worktrees) | `mcp/client.py` · `auth/` · `db.py` · `matrix/render+throttle` · `sites.py` | Sonnet ×4 | Unit tests pass against fixtures |
| 2r | Security review of `auth/` and `web.py` | | **Opus** | No open findings |
| 3 | Features (parallel worktrees) | chat · investigations · alerts+webhooks · unfurl+reactions | Sonnet ×4 | Feature tests pass |
| 4 | Integration & hardening | End-to-end in Docker (Synapse + maubot + real DD test org); restart/resume; E2EE room test | Sonnet | Manual checklist passes |
| 4r | Final review | `/code-review high` on the whole repo; security review | **Opus** | — |
| 5 | Docs & release | README (setup per site, webhook how-to, threat model), CHANGELOG, `.mbp` release workflow | Haiku (Sonnet edits threat model) | v0.1.0 tagged |

### Phase 2 review notes: requirements for Phase 3 wiring

Phase 2 status: 120 offline tests pass, lint is clean, and the Opus review of `bitsai/auth/` found nothing blocking.

The command and web layers must also:

1. **Validate a PAT before saving it.** Call `GET /api/v2/current_user` with it, and only store it on a 200. Redact the user's message in all cases, and refuse outside a verified 1:1 DM.
2. **Rate-limit `!bits login` per user**, for example 3 per 10 minutes. Each call triggers a client registration request to Datadog.
3. **Confirm with a fallback.** If `dd_user_handle` is `None`, the confirmation still goes out, saying "Linked (identity unknown)". It must never be skipped.
4. **Keep the OAuth callback page minimal.** It shows "Done, return to Matrix", or a generic error that never echoes `state`, `code` or Datadog errors. It sets `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
5. **Warn about debug logging in the docs.** At DEBUG the database driver logs bound parameters, which include encrypted blobs and pending `state` values. Don't run DEBUG in production.
6. **Later, possibly:** check the RFC 9207 `iss` callback parameter, and support key rotation for `token_encryption_key`.

### Phase 3 status: complete (2026-11-12)

228 offline tests pass and lint is clean. `mbc build` produces an `.mbp`.

### Phase 4 checklist: live checks against the sandbox org and a real homeserver

1. **OAuth through the real maubot `public_url`:**
   - Confirm a non-localhost callback URL is accepted after login.
   - Confirm a sign-in link opened by someone else, or after use, is rejected.
2. **Real monitor webhook:**
   - Does an alert title with quotes still produce valid JSON?
   - How many digits does `$ID` have? `MonitorAlertRef` needs at least 16, so 🔍 depends on this.
   - Check that `$DATE` is in milliseconds.
3. **Chat end to end:** thread follow-ups, and `requires_action` if any prompt triggers it.
4. **Investigation end to end:** start it, watch the status edits, restart maubot mid-run and confirm it resumes, then steer.
5. **Encrypted DM and encrypted room:** check the bot can read and reply in both.
6. **Postgres:** run with `plugin_databases.postgres` to confirm the boolean and upsert SQL work there.
7. **MCP-triggered investigation with "MCP Write Access" off.** This is a write, so it needs your approval first.
8. **Small fixes:**
   - Store the alert title and status so the 💬 prompt can include them.
   - Exclude `__pycache__` from local builds.

### Cost tactics

1. **Contracts before code.** Opus writes short interface files. Each Sonnet task's context is then just the interface, its fixtures and one module, not the whole repo.
2. **Recorded fixtures from Phase 0.** All later tests run offline, so no model burns tokens on live API debugging, and the cheap models get deterministic feedback.
3. **`CLAUDE.md`** records the conventions and the maubot gotchas listed above, so later sessions don't re-research them.
4. **Parallel worktrees** for independent modules. Each task brief names the files to read and the tests that must pass.
5. **Escalate by exception.** A Sonnet task that fails its tests twice goes to Opus with only the failing diff and test output.
6. **Haiku** handles boilerplate, fixture formatting, docs and changelogs. Nothing security-sensitive.
7. **Tiered reviews.** Opus reviews only `auth/`, `web.py`, token storage and the final diff. Everything else gets `/code-review medium`.

## Open questions / risks

1. ~~Bits Chat via MCP is unverified.~~ **Resolved: it works.** Remaining risk: the MCP server is "under significant development", so its tool schemas may change. Mitigations:
   - Keep schema-dependent code inside `backend/mcp.py`.
   - Add a start-up check that compares `tools/list` with the expected schemas and warns on drift.
2. ~~OAuth for self-hosted open source.~~ **Resolved:** registration accepts any callback URL, but `/authorize` only accepts loopback URLs plus whatever the org admin has listed under Organization Settings > MCP Server > MCP OAuth Redirect URLs. So the default (`auth.oauth_redirect: auto`) is a direct callback to the maubot's `public_url`, which the org admin must list. Loopback paste-back (`paste`) is the fallback when that isn't possible (`docs/FINDINGS.md` §5).
3. Investigation status values are only partly known. Treat unknown statuses as non-terminal and time out after N minutes.
4. FED sites are unsupported by MCP. Document as unsupported for chat and use REST-only investigations there, if the API exists.
5. Bits quota and cost are borne by each deployer's org. Rate limits are configurable.
6. Licence: AGPL-3.0, matching most maubot plugins, or MIT/Apache-2.0. Your call.
