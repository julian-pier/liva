# LIVA MCP – controlled access

Install the repository root before this optional MCP package. The root package
ships the shared `liva_memory` runtime used by the memory tools:

```bash
python -m pip install -e .
python -m pip install -e ./liva_mcp
```

The MCP exposes a loopback-only authenticated bridge to the canonical LIVA read
and act facades. `liva_coach_read` and `liva_coach_act` therefore use the same
production business logic as LIVA Custom GPT without importing Flask or opening
the application databases for general MCP writes.

Remote runtime configuration is generated outside the repository with `liva-mcp-generate-remote-config --base-url https://HOST` (or the legacy `https://HOST:PORT`). It must never reference the LIVA `.env` or existing LIVA write credentials. Memory reads use a dedicated GET-only configuration when supplied; live `liva_capture_memory` additionally requires a separate `LIVA_MCP_MEMOS_WRITE_TOKEN`.

Dynamic registration accepts only ChatGPT HTTPS callbacks, `liva.read`, optional `liva.write`, and optional `offline_access`. A client with new write access must authorize again.

This isolated module exposes explicitly registered LIVA read and controlled-write tools over
MCP stdio. It does not import Flask, `app.py`, `ai.actions_v2`, database
connection helpers, schema initializers, sync jobs, or application write
services. Canonical actions cross a fixed loopback HTTP boundary with a
dedicated bearer credential.

## Safety model

- Application SQLite is opened with URI `mode=ro`, `PRAGMA query_only=ON`, and a restrictive SQLite authorizer. The separate MCP overlay is the only writable SQLite file.
- The public tool names remain static. The canonical
  `liva_coach_read`/`liva_coach_act` pair is deliberately forward-compatible:
  call `liva_coach_read(mode="capabilities")` for the current modes, commands,
  payload hints, confirmation rules, and dry-run support. There is no arbitrary
  endpoint, SQL, or function dispatch.
- The server implements MCP JSON-RPC over stdin/stdout only. It does not listen
  on a TCP port.
- usememos reads use HTTP GET. Live capture uses only the dedicated
  `LIVA_MCP_MEMOS_WRITE_TOKEN` and never falls back to a general-purpose token.
- Errors pass through central redaction and never include tracebacks.

## Local start

From this directory, without installing anything:

```bash
PYTHONPATH=src ../venv/bin/python -m liva_mcp.server
```

Supported MCP methods are `initialize`, `notifications/initialized`,
`ping`, `tools/list`, and `tools/call`.

## Tools

- `liva_daily_snapshot`
- `liva_training_state`
- `liva_training_plan`
- `liva_recovery_snapshot`
- `liva_nutrition_snapshot`
- `liva_weight_state`
- `liva_runs_state`
- `liva_coach_read`
- `liva_coach_act`
- `liva_memory_search`
- `liva_memory_read`
- `liva_post_daily_note`
- `liva_upsert_weight`
- `liva_post_nutrition_context`
- `liva_upsert_memory_entry`
- `liva_capture_memory`
- `liva_post_daily_flag`
- `liva_post_training_rawlog`
- `liva_operator_plan`
- `liva_operator_execute`

The typed state tools query the existing LIVA SQLite structures directly;
`liva_coach_read` and `liva_coach_act` cross the authenticated loopback bridge
to the canonical production facade.
The stable pair covers calendar range/search, movement-library and nutrition
plan/template detail, logged-workout corrections and deletion, live training
plan mutations, meal/weight writes, and create/edit/delete cardio sessions.
Adding another validated command inside an existing coach domain does not
change the MCP tool list or OAuth identity.

Historical analysis uses the same stable pair. Read
`analysis_phase_overview` first for a bounded cross-domain weekly summary, then
`analysis_phase_detail` with one of `days`, `training`, `exercises`, `nutrition`
or `recovery`. Detail results include offset pagination and explicit payload
size metadata, so clients can zoom without loading an entire history at once.
The phase contract also requires a targeted Memory V2 recall before
interpretation. Saving a resulting pattern is never automatic: after explicit
user confirmation it uses `liva_memory_begin` and `liva_memory_finish` with the
range, metrics, statement and exceptions preserved in the source.

`checkin_today` includes `weekly_feedback` on Mondays for the prior completed
Monday–Sunday week. `weekly_feedback` can also read any earlier completed week
directly by `week_start` or `week_end`; it remains a compact GPT payload and
does not create another frontend review surface.

The same stable pair exposes the local `skill` domain. Its current Grill Me
vertical slice provides registry/load/session reads and start, record-turn,
checkpoint, pause, resume and stop actions. Skill sessions are revisioned and
idempotent, remain pinned to the loaded skill version, use bounded Memory V2
context, and archive only after a verified final checkpoint. Session sources
are not automatically promoted to Living Wiki knowledge.
Memory search/read call the usememos v1 GET endpoints when configured; the
controlled upsert remains an explicit overlay staging write, while capture is
the narrow authenticated private-Memos write path.

The three `*_snapshot` tools deliberately aggregate selected read-only sources.
They expose source-level freshness and parity metadata and are not equivalent to
the complete canonical LIVA daily, recovery, or nutrition states. See
`docs/canonical_parity.md` for the exact gap analysis.

## Configuration

- `LIVA_MCP_REPO_ROOT`: optional repository root override, mainly for tests.
- `LIVA_MCP_MEMOS_BASE_URL`: optional usememos base URL.
- `LIVA_MCP_MEMOS_READ_TOKEN`: optional dedicated read-only bearer token.
- `LIVA_MCP_MEMOS_WRITE_TOKEN`: optional dedicated bearer token for capture;
  never reuse the LIVA `.env` token here.
- `LIVA_MCP_ACTIONS_BASE_URL`: loopback LIVA origin, normally
  `http://127.0.0.1:5000`.
- `LIVA_MCP_ACTIONS_TOKEN`: dedicated internal bearer credential accepted by
  the canonical LIVA Actions facade. Keep it outside the repository.
- `LIVA_MCP_ACTIONS_TOKEN_FILE`: preferred private 0600 token file. It is used
  when `LIVA_MCP_ACTIONS_TOKEN` is unset and can be shared by the two local
  systemd services without copying the general LIVA AI credential.

No `.env` file is loaded by this module.

## Tests

```bash
../venv/bin/python -m pytest -q
```

## Remote OAuth smoke test

The remote server is an OAuth resource server. `LIVA_MCP_REMOTE_BASE_URL` and
`LIVA_MCP_OAUTH_ISSUER` must be the same HTTPS origin; the protected resource is
that origin plus `/mcp`. Required environment variable names are documented in
`deploy/README.md`; their values must remain outside the repository.

After obtaining an OAuth access token through the authorization-code/PKCE flow,
use a shell environment variable so it is not stored in shell history:

```sh
curl --fail-with-body -X POST "$LIVA_MCP_REMOTE_BASE_URL/mcp" \
  -H "Authorization: Bearer $LIVA_MCP_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1"}}}'
```

The journal records only fixed validation categories (for example `issuer`,
`audience`, `signature`, `expired`, or `malformed`) and never token contents.

## Unified context snapshot

`liva_context_snapshot` is a read-only `liva.read` tool that returns a compact,
machine-readable overview of current weight, training, recovery, nutrition,
running and the locally available calendar. It has no parameters and never
creates decisions, tasks, events or writes. Missing, stale, unavailable, and
not-configured components are reported under `data_quality`; tasks and system
status are intentionally marked not configured unless a safe local reader is
added.

The response has stable `summary`, `weight`, `training`, `recovery`,
`nutrition`, `running`, `tasks`, `calendar`, `system`, and `data_quality`
sections. Example: `{ "summary": {"status":"ok"}, "data_quality": {"missing":[],"stale":[],"warnings":[]} }`.

## Agent instructions and enforcement boundary

`src/liva_mcp/agent_policy.md` is the single runtime orchestration policy. It is
packaged with the module and returned as MCP server instructions by both the
remote FastMCP transport and the stdio `initialize` response. No repository
`SKILL.md` or legacy GPT prompt is loaded by this MCP runtime.

The server technically enforces schemas, scopes, dry-run/write validation,
idempotency, readbacks, freshness fields, and temporal response contracts. It
cannot force an external ChatGPT host to call a tool before answering because it
does not receive model turns or model responses. The universal pre-answer
`liva_context_snapshot` rule is therefore instruction-enforced unless the host
adds mandatory tool-call middleware.
