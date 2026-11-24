# Phase 0 spike findings (2026-11-12, site datadoghq.com, test org)

Public endpoints only. Raw transcripts were captured locally by throwaway spike scripts (not in this repo); redacted fixtures in `tests/fixtures/`.
Scripts: `common.py` (MCP client prototype + redaction), `rest.py`, `s1..s7_*.py`, `build_fixtures.py`.

## Capability table

| Capability | Works? | Auth used | Notes |
|---|---|---|---|
| MCP initialize / tools/list (`toolsets=all`, 498 tools) | Yes | PAT Bearer | protocolVersion `2025-06-18`; caps `tools/prompts/resources`; serverInfo `datadog-mcp 1.0.0`; `Mcp-Session-Id` returned on initialize; `notifications/initialized` -> 202 |
| `?subdomain=<org>` variant | Yes, no observable difference | PAT Bearer | same 498 tools; investigation links use `https://<subdomain>.datadoghq.com/...` regardless |
| Chat toolset (`toolsets=chat`) | Yes | PAT Bearer | 4 tools. NOT included in `toolsets=all` (must be requested explicitly) |
| Chat send + poll + follow-up | Yes | PAT Bearer | async, see Q2 |
| `trigger_general_investigation` via MCP | Not called | - | REST used instead |
| REST `POST /api/v2/bits-ai/investigations` | Yes (200) | PAT Bearer | no unstable-ops flag needed on raw HTTP |
| REST `GET /api/v2/bits-ai/investigations[/{id}]` | Yes | PAT Bearer | |
| MCP `get_bits_ai_investigation`, `search_investigations` | Yes | PAT Bearer | much richer than REST |
| `/api/v2/current_user`, `/api/v2/permissions`, `/api/v2/users/{id}/permissions`, `/api/v2/personal_access_tokens` | Yes | PAT Bearer | PAT list reveals the token's `scopes` |
| `/api/v1/validate` | 403 | PAT Bearer | validate only supports API keys; do not use for PAT checks |
| `GET /api/v1/graph/snapshot` | Yes (200) | PAT Bearer | returns `snapshot_url` (png on `app.datadoghq.com`), `graph_def`, `metric_query` |
| REST getters: monitor, dashboard, notebook, incident | Monitor/dashboard/notebook 200; incident list 200 (empty), get-by-id 404 for unknown id | PAT Bearer | no incidents in org, so incident shape not captured; MCP `search_datadog_incidents` returned TSV (0 matches) |
| MCP getters (`search_datadog_monitors`, `get_datadog_dashboard`, `search_datadog_incidents`) | Yes | PAT Bearer | |
| MCP OAuth discovery (RFC 9728 + 8414) | Yes | none | see Q5 |
| MCP DCR | Yes, both redirect URIs accepted (201) | none | see Q5 |
| Unauthenticated MCP POST | 401 + `WWW-Authenticate` | none | |

## 1. MCP with PAT
- `POST https://mcp.datadoghq.com/v1/mcp?toolsets=...` with `Authorization: Bearer <PAT>`, `Accept: application/json, text/event-stream` works.
- **Response framing is mixed**: `initialize` and `tools/list` reply `application/json`; **`tools/call` replies `text/event-stream`** (`event: message\ndata: {jsonrpc...}`), one event holding the final response. No progress notifications seen. A client must handle both. Sample wire in `tests/fixtures/mcp_tools_call_sse_wire.txt`.
- Session: `Mcp-Session-Id` header (`mcp-session-<uuid>`) is returned on initialize; client resends it. We also send `MCP-Protocol-Version`.
- Tool results are `{"content":[{"type":"text","text":"<JSON string>"}]}`; the payload is a JSON string that must be parsed a second time (some tools return TSV/XML-ish `<METADATA>` text instead).
- **Every tool requires a `telemetry: {intent: str}` argument** (`required` in the schema).
- `initialize.instructions` says: "You cannot call write tools because your organization has MCP Write Access disabled" (org setting). `send_message_to_chat` (readOnlyHint=false) still worked; so chat is not treated as a "write tool" by that gate. Whether `trigger_*` MCP tools are blocked by it was not tested (REST used).
- `toolsets=all` = 498 tools (many writes: create/update/delete). Full list: `tests/fixtures/mcp_tools_list_all.json` (compact). Tool metadata `_meta.toolsets` tags each tool; `annotations` carry readOnlyHint etc.
- Relevant toolsets observed:
  - `chat` (4): `send_message_to_chat`, `get_chat_message_result`, `get_chat_conversation_history`, `list_chat_conversations` (`_meta.toolsets: [chat, assistant]`).
  - `investigator` (11): `trigger_bits_ai_investigation`, `trigger_general_investigation`, `trigger_incident_investigation`, `get_bits_ai_investigation`, `search_investigations`, `get_investigations_from_incident_id`, `steer_bits_ai_investigation`, `resume_bits_investigation`, `investigate_datadog_error_tracking_issue`, plus `list_datadog_skills`, `load_datadog_skill`.
  - `agent-builder` (11): `run_datadog_bits_agent` (async; returns `conversation_id` + status queued/running; NOT called), `get_datadog_bits_agent_conversation_events`, `list_datadog_bits_agents`, create/update/remove variants.
  - `core`: `search_datadog_monitors`, `get_datadog_dashboard`, `search_datadog_dashboards`, `get_datadog_incident`, `search_datadog_incidents`, `get_datadog_notebook`, `search_datadog_notebooks`, `get_datadog_metric`, `execute_code`, etc. Note `core` also includes `create_datadog_notebook`, `delete_datadog_notebook`, `edit_datadog_notebook`.
  - Security note for the plugin: request only `toolsets=chat` (and `investigator`) and allow-list tool names client-side.

## 2. Chat semantics (verified)
- `send_message_to_chat({message, conversation_id?, pending_action_id?, telemetry})` returns immediately (~0.2s) with
  `{conversation_id, task_id, status:"queued", message:null, error_code, error_message, pending_action, next_action:{tool:"get_chat_message_result", arguments:{task_id,conversation_id}, poll_after_seconds:2}}`. Tool description says "depending on org config" it may instead return the completed response synchronously, so the client must handle both `status=completed` on send and queued/running.
- `get_chat_message_result({task_id, conversation_id, telemetry})` **long-polls server-side**: first poll blocked ~16s (Q1) and ~4s (Q2) and returned `status:"completed"` directly. We observed only `queued` (send) and `completed`; no `running` state and **no streaming/partial content** (`message` is null until completed). Documented states: queued, running, completed, failed, cancelled, requires_action (with `pending_action {id, target_tool, description, arguments}`; approve/deny by calling send with the same conversation_id and `pending_action_id`). Failed/cancelled -> `error_code`/`error_message`.
- Latency: Q1 ~19s end-to-end (4 tool calls), follow-up ~7s.
- Conversation: **follow-up with the returned `conversation_id` continued the same conversation** (answer correctly referred to "the first one"; same id returned). New `task_id` per message.
- Answer format: Markdown string in `message`. **Links are relative** (e.g. `[name](/monitors/<id>)`), so the plugin must prefix `https://app.<site>` (or org subdomain). No citation structure in the final message.
- `get_chat_conversation_history`: `data.attributes.messages[]` with `role`, `message_id`, `agent_id` ("command"), `created_at` (ms), `content.type` in {`markdown_fragment`, `tool_call` (title e.g. "Fetching monitors...", `metadata.name/input`), `tool_response` (`status`, `metadata.output`), `thinking` (incl. `encrypted_content`)}, plus `results.usage {tokens_used,max_tokens}`. Usable to render "progress" steps while a task is running (not tested mid-run) - history is the only place intermediate tool steps are visible.
- `list_chat_conversations`: `data.attributes.conversations[] {id, conversation_id, updated_at, title, owner_user_uuid, shared_expires_at, is_pinned}`; conversations are per-user (the token's owner).
- Chat required `assistant_access` (and presumably `mcp_read`) - see permissions.

## 3. Investigations
- REST trigger accepted PAT Bearer, no extra headers.
  Request: `{"data":{"type":"trigger_investigation_request","attributes":{"trigger":{"type":"general_investigation","general_investigation":{"description":"...", "tags":[...]?, start/end?}}}}}`
  Response 200: `{"data":{"id":"<trigger-id>","type":"trigger_investigation_response","attributes":{"investigation_id":"<uuid>"}}}`.
- Poll `GET /api/v2/bits-ai/investigations/{id}` (200 right away): `data.attributes = {agent_run_workflow_id, conclusions[], status, strategy:"react", title, latest_finished_at (only when finished)}`; `links.self` = `https://<subdomain>.datadoghq.com/bits-ai/investigations/<id>` (deep link; org subdomain used).
- **Statuses observed while polling: `in progress` (with `conclusions: []`) then `conclusive`**. Total ~193s for this simple prompt. Other values (from `search_investigations` facet docs, not observed): `failed`, `needs direction`, `inconclusive`. Note the space in `in progress`. Treat unknown as non-terminal.
- REST conclusion shape: `[{title, summary, description (markdown, with ## Evidence/Analysis headings), attempt_number}]`. No citations/links beyond text. Backoff poll 10s->60s was fine, no 429.
- REST list: `GET /api/v2/bits-ai/investigations?page[limit]=N` -> `{data:[{id,type,attributes:{status,title}}], meta.page{total,...}, links}`.
- MCP `get_bits_ai_investigation` (60 KB for this one) returns `data.attributes.response.investigation` with `status`, `conclusions[]` (confidence int, summary/description/message as lists of `{text,type:"MARKDOWN"}`, `<tool id="TOOL-1"/>` citation tags, monitor_details), `action_results[]` (tool_citation_id, tool_summary...), `hypotheses`, `react_investigation_steps`, `remediations`, `timings`, `narrative`, `initiated_by_uuid`, `initiation_type`. Richer than REST; better source for rendering evidence, but large and the schema differs (text blocks vs strings).
- MCP `search_investigations` returns facets + results (id/title/status/timestamps/trigger).
- OpenAPI (public spec) says POST needs `bits_investigations_write` (+`monitors_read` for monitor_alert_trigger); GET/list need `bits_investigations_read`; security: API key+app key OR OAuth scope.
- Since REST works with PAT, no fallback to DD-API-KEY/APP-KEY was needed (not tried).

## 4. Permissions / scopes
- `/api/v2/current_user` -> user with role "Datadog Admin Role" and ~390 permission objects in `included`. Relevant names: `assistant_access`, `mcp_read`, `mcp_write`, `bits_investigations_read`, `bits_investigations_write`, `agent_builder_read/write/run`, `bits_settings_*`, `monitors_read`, `dashboards_read`, `notebooks_read`, `incident_read`, `metrics_read`/`timeseries_query`, `events_read`.
- **`GET /api/v2/personal_access_tokens` exposes a token's `scopes[]`** (named like permissions) - so a plugin can display/validate the scopes of a user-supplied PAT. This PAT carried all scopes.
- Minimum scopes a reduced test needs (NOT tested; create a PAT/OAuth client with only these):
  - Chat: `assistant_access` + `mcp_read` (likely); the chat agent then calls tools under the same identity, so data visibility also needs `monitors_read`, `dashboards_read`, `logs_read_data`, etc.
  - Investigations REST: `bits_investigations_read` / `bits_investigations_write` (+ `monitors_read` for monitor-triggered).
  - Snapshot: not documented here; PAT with everything worked. Test with `metrics_read`/`timeseries_query` only.
  - Unfurl getters: `monitors_read`, `dashboards_read`, `notebooks_read`, `incident_read`.
- Org-level setting "MCP Write Access" is disabled in this org (stated in MCP instructions).
- Docs-string gating: no scope info is returned in responses/headers of normal calls (only `X-RateLimit-Limit: 60` on snapshot).

## 5. OAuth / DCR
- `GET https://mcp.datadoghq.com/.well-known/oauth-protected-resource` -> `{resource:"https://mcp.datadoghq.com", authorization_servers:["https://mcp.datadoghq.com/v1/mcp"], bearer_methods_supported:["header"]}`; path variant `/.well-known/oauth-protected-resource/v1/mcp` -> `resource: https://mcp.datadoghq.com/v1/mcp`, same AS.
- Unauthenticated POST -> 401, `WWW-Authenticate: Bearer resource_metadata="https://mcp.datadoghq.com/.well-known/oauth-protected-resource/v1/mcp"`, body `{"errors":["Unauthorized"]}`.
- AS metadata (works at `/.well-known/oauth-authorization-server` and `.../oauth-authorization-server/v1/mcp`; **404 for openid-configuration** variants and for the path-suffix-after variant `/v1/mcp/.well-known/...`):
  issuer `https://mcp.datadoghq.com/v1/mcp`; authorization_endpoint `https://app.datadoghq.com/oauth2/v1/authorize`; token_endpoint `https://app.datadoghq.com/api/v2/oauth2/token`; registration_endpoint `https://app.datadoghq.com/api/v2/oauth2/register`; response_types `code`; grant_types `authorization_code`, `refresh_token`, `urn:ietf:params:oauth:grant-type:jwt-bearer` (+ ID-JAG profile); PKCE `S256`, `pkce_required: true`; scopes_supported `["mcp_all"]`; token_endpoint_auth_methods `["none"]` (public clients only); `oauth_version: "2.1"`.
  Note the issuer URL path (`/v1/mcp`) differs from the endpoints' host (`app.<site>`); for other sites the plugin must derive `app.<site>` endpoints from discovery rather than hard-code.
- **DCR works anonymously (no auth) and accepted both redirect URIs: HTTP 201** for `https://example.invalid/_matrix/maubot/plugin/bitsai/oauth/callback` and for `http://localhost:8765/callback`. Request: `{client_name, redirect_uris, grant_types, response_types, token_endpoint_auth_method:"none", scope:"mcp_all"}`. Response echoes `client_id, client_name, redirect_uris, token_endpoint_auth_method, grant_types, response_types` (no secret, no `scope`, no `client_id_issued_at`).
- Surprise: **both registrations returned the same `client_id`** (same `client_name`), and the second response lists only the localhost URI, so registration seems keyed by client_name (upsert, possibly replacing the first redirect URI list). Implication: a plugin must use a unique/stable `client_name` per deployment and re-register (or persist client_id) per deployer; two deployments sharing a name could collide. Not verified whether the first redirect URI still validates at /authorize (consent step deliberately not performed).
- Browser authorization/consent and token exchange were NOT performed, so: unverified whether authorization with a non-localhost https redirect completes, and what the issued token's scope/TTL/refresh behavior is.

- **2026-11-12 (`s9_redirect.py`): /authorize ignores DCR'd redirect URIs; it uses a fixed allowlist.**
  An https callback (`https://<host>/bitsai/oauth/callback`) registered via DCR immediately before
  /authorize was rejected *after login* with `invalid_request - Mismatching redirect URI.`. With a
  logged-in browser, both a DCR-registered loopback URI (`http://localhost:29316/...`) and an
  **unregistered** loopback URI (`http://localhost:8765/some/other/callback`) reached the consent
  page. Unauthenticated, /authorize 307s to login for every redirect_uri (validation happens after
  login), so this can't be checked headlessly. Implication: OAuth sign-in only works with a loopback
  redirect; a maubot on a public https URL must use a loopback redirect + paste-back (or PATs).
  Each DCR also echoes only the latest redirect list for the shared client_id.

- **2026-11-24 (docs, not re-tested live): the "fixed allowlist" is an org setting.** Datadog's
  [MCP setup docs](https://docs.datadoghq.com/mcp_server/setup/) describe an admin-managed list,
  Organization Settings > MCP Server > **MCP OAuth Redirect URLs** ("enter the URL, and select
  Add URL"). That explains the result above: loopback URIs are accepted without listing, and our
  https callback was rejected because the spike org hadn't listed it. Not documented: matching
  rules (exact vs. prefix/wildcard), and which role may edit it beyond "Administrators".
  Implication: a public https callback works once an org admin lists the exact callback URL.
  The plugin's `auto` mode uses that callback; paste-back (`paste`) is the no-setup fallback. Related org toggles: **MCP
  Access** (off = no MCP for anyone) and **MCP Write Access** (off = write tools hidden); users
  need `mcp_read`/`mcp_write` (Datadog Standard Role has both).

## 6. Snapshot and unfurl getters
- `GET /api/v1/graph/snapshot?metric_query=avg:system.cpu.user{*}&start=..&end=..` with PAT Bearer: 200, `snapshot_url` = `https://app.datadoghq.com/api/v2/snapshot/view/public/2y/<org-uuid>/<ts>-<uuid>.png` (public, unauthenticated image URL, 2y path segment).
- Monitor `GET /api/v1/monitor/{id}`, dashboard `GET /api/v1/dashboard/{id}`, notebook `GET /api/v1/notebooks/{id}`: 200. Incident `GET /api/v2/incidents/{id}`: 404 `{"errors":[{"title":"Generic Error","detail":"incident not found"}]}` (no incidents). MCP `get_datadog_dashboard` works and returns `url` using the org subdomain.

## Surprises / design implications
1. Chat toolset is not in `toolsets=all`; request `chat` explicitly. Poll is a server-side long-poll (up to ~20s) - set aiohttp read timeout >= 30s and no tight loop; no partial text, so "streaming edits" are impossible from chat; use `get_chat_conversation_history` (tool_call/tool_response entries) for progress hints only (unverified mid-run).
2. `tools/call` replies are SSE; others JSON.
3. Chat answers contain relative links.
4. Investigation status strings have spaces ("in progress").
5. REST investigations work with PAT Bearer; no preview/unstable flag needed over raw HTTP.
6. DCR is open, accepts https non-localhost redirect URIs; same client_id for the same client_name.
7. PAT scopes are introspectable via `/api/v2/personal_access_tokens`.
8. Every MCP tool needs `telemetry.intent`.
9. Org has "MCP Write Access" disabled, yet chat send worked.

## Write actions actually performed (for cleanup)
- Bits Chat: 2 messages (budget 3), one conversation: `a04513d9-f4ea-47a0-8059-cebca88237e8` (title "Which monitors in this org are currently alerting? Answer briefly."). Tasks `8ac770fa-a129-5fb4-b3ae-23f800d98bcf`, `bf11ba26-5770-58ac-85b6-8ec83bfef7d5`.
- Investigation: 1 via REST POST. investigation_id `d8dafc19-8614-4fee-8077-1fa10d9d655d` (trigger id `c012a4af-9dcc-4a23-aac1-d45132fbddae`), conclusive.
- DCR: 2 POSTs (+5 on 2026-11-12 for s9, same client_name) to `https://app.datadoghq.com/api/v2/oauth2/register`; both returned client_id `51ea5567-28b8-4c79-9a7d-e70239b14811` (client_name "maubot-bitsai spike (test, safe to delete)").
- Graph snapshot: 1 GET `/api/v1/graph/snapshot` (renders a public PNG).
- Everything else was GET / read-only MCP calls. No MCP write tools or `run_datadog_bits_agent` were called. Note: `send_message_to_chat` also writes to Bits conversation history.
