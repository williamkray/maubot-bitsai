# maubot-bitsai

MIT-licensed maubot plugin bringing Datadog Bits AI (chat, investigations, alerts, unfurls) to Matrix.
Design and phase plan: `PLAN.md`. Verified API behaviour: `docs/FINDINGS.md` + `tests/fixtures/`.

## Ground rules
- **Public Datadog surfaces only.** Build only on documented/public endpoints and the public MCP
  server. Never copy internal Datadog code or undocumented internal APIs into this repo.
- **Contracts are frozen:** `bitsai/backend/base.py`, `bitsai/auth/base.py`. Implement against
  them; propose changes in your report instead of editing them (except when your task says so).
- **No new runtime deps.** maubot doesn't install plugin deps. Allowed: stdlib, mautrix, aiohttp,
  yarl, attrs, ruamel.yaml, jinja2, commonmark, asyncpg/aiosqlite (via mautrix), pycryptodome
  (`Crypto.Cipher.AES`, present in the maubot Docker image). Do not use the `mcp` SDK, pydantic,
  httpx, requests, or `cryptography`.
- **Never log or render secrets** (tokens, PATs, codes, PKCE verifiers). Use `repr`-safe types.
- Live Datadog calls are only for spikes the user has approved. Tests must be offline, using fixtures.

## Verified Datadog facts (2026-11, US1)
- MCP: `https://mcp.<site>/v1/mcp?toolsets=core,chat,investigator` — `chat` is NOT in `all`.
  Protocol `2025-06-18`; keep `Mcp-Session-Id`. `initialize`/`tools/list` reply JSON,
  `tools/call` replies **SSE**. Tool result = JSON string in `result.content[0].text`.
  Every tool requires `telemetry: {"intent": "..."}`.
- Chat: `send_message_to_chat` -> `{status: queued, task_id, conversation_id, next_action}`;
  `get_chat_message_result(conversation_id, task_id)` long-polls ~4–16 s. No partial text.
  Statuses: queued/running/completed/requires_action/failed/cancelled. Approve a pending action by
  `send_message_to_chat(message=<natural-language approval>, pending_action_id=..., conversation_id=...)`.
  Answers are Markdown with **relative** links -> prefix `https://<subdomain or app>.<site>`.
- Investigations: statuses seen `in progress`, `conclusive`; also documented `needs direction`,
  `inconclusive`, `failed`. Unknown = non-terminal. MCP getter output is ~60 KB — summarise.
- Auth: PAT and OAuth access tokens both work as `Authorization: Bearer` on MCP **and** REST.
  MCP OAuth: anonymous DCR (`https://app.<site>/api/v2/oauth2/register`) always returns the same
  shared client_id; PKCE S256 required; send `resource=https://mcp.<site>/v1/mcp`; request scope
  `mcp_all`, but the granted token carries ALL the user's permissions. Access token = JWT, 3600 s.
  Refresh token is long-lived and NOT rotated. Re-run DCR with our redirect_uri before each login.
  **/authorize accepts loopback redirect URIs by default** (any `http://localhost` port/path);
  any other URI must be in the org's admin-managed allowlist (Organization Settings > MCP Server >
  MCP OAuth Redirect URLs), else "Mismatching redirect URI". DCR registration doesn't count (§5).

## maubot gotchas
- `database_type: asyncpg` (default is legacy sqlalchemy). SQL must work on SQLite and Postgres
  (`$1` placeholders; use the `Scheme` arg in upgrades when dialects differ).
- Web routes (`@web.get/post`) are **unauthenticated**: validate OAuth state / webhook tokens.
  Public URL = `<public_url>/_matrix/maubot/plugin/<instance_id>/...`.
- `evt.respond()` sends `m.notice` by default; `in_thread=True` for threads; `edits=event_id` for
  edits (no built-in throttle — use `matrix/throttle.py`). Edits can't change thread/reply.
- Cancel `asyncio` tasks in `stop()`; prefer `self.sched`.

## Layout
`bitsai/` plugin package (see PLAN.md "Module layout"), `tests/` pytest (asyncio_mode=auto),
`docs/` reference notes, `spike/` local-only throwaway scripts (gitignored), `docker/` local
Synapse+maubot.

## Commands
- `.venv/bin/ruff check . && .venv/bin/pytest -q`
- `mbc build` (plugin .mbp) — `mbc build -u` to upload to a logged-in maubot.
  **Delete the output file first** (`rm -f /tmp/bitsai.mbp`): if it exists, `mbc build` waits on
  an interactive overwrite prompt and hangs in non-interactive shells. Run with `</dev/null`.
- Local dev deployment: Docker container `maubot-bitsai` (data in `docker/data/maubot/`,
  gitignored), instance id `bits`, admin password in `~/.config/maubot-bitsai/maubot_admin.txt`.
  Upload with `POST /_matrix/maubot/v1/plugins/upload?allow_override=true` (restarts the instance).

## Working style (cost)
Tasks are delegated per PLAN.md: Opus = contracts/security/review, Sonnet = modules, Haiku = boilerplate/docs.
Keep task context small: read the contract, your module, its fixtures — not the whole repo.
