# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo layout

Single Go project under `droids-mem/`. Root holds only scratch files (`droids-name.yml`, `files/`). Treat `droids-mem/` as working dir for all build, test, run.

## Build / test / run

All Go commands from `droids-mem/`:

```
go build ./cmd/droids-mem        # single binary: CLI + `serve` (MCP bridge) + `ensure-server`
go run ./cmd/droids-mem <subcmd> # run without building
go test ./...                    # all tests
go test ./internal/store -run TestSave_DedupesByFingerprint   # single test
go test -count=1 ./...           # bypass test cache
```

Frictionless startup (single command, zero env required):

```
droids-mem ensure-server         # ping /healthz, spawn `droids-mem serve` detached if down
droids-mem serve                 # foreground MCP bridge (used by ensure-server)
```

E2E tests (both in `cmd/droids-mem/`):
- `e2e_test.go` — invokes the built CLI end-to-end.
- `serve_e2e_test.go` — spawns `droids-mem serve` on an ephemeral port and drives it via JSON-RPC; covers auth, tool surface, session minting, dedupe, SIGTERM graceful shutdown.

Both suites isolate `DROIDS_MEM_DB` and `DROIDS_MEM_HOME` per test to avoid clobbering the local DB or token file.

## Runtime env

- `DROIDS_MEM_DB` — DB path. Default `~/.droids-mem/mem.db`. Always set this to a tempfile in tests.
- `DROIDS_MEM_HOME` — state directory (token, pid, log). Default `~/.droids-mem`. Override in tests for isolation.
- DB auto-creates parent dir (`0o700`) and applies pragmas: WAL, foreign_keys=ON, synchronous=NORMAL.
- Schema + 3 FTS sync triggers applied on every `Open()` (idempotent via `IF NOT EXISTS`). `updated_at >= created_at` enforced via CHECK constraint, not a trigger.

MCP bridge (`droids-mem serve` / `droids-mem ensure-server`):
- `DROIDS_MEM_MCP_TOKEN` — bearer token for `/mcp`. Optional. If unset, `internal/state.LoadOrCreateToken` reads `~/.droids-mem/token` or generates a fresh `tok_<ULID>` persisted 0600. Constant-time compare on every request.
- `DROIDS_MEM_MCP_ADDR` — bind address. Default `:7777`.
- `DROIDS_MEM_MCP_ENDPOINT` — MCP path. Default `/mcp`. `/healthz` is exposed unauthenticated for liveness probes.

State directory layout (`~/.droids-mem/`):
- `mem.db` (+ `mem.db-wal`, `mem.db-shm`) — SQLite store.
- `token` — bearer token (mode `0600`).
- `mcp.pid` — PID of the most recent `ensure-server` spawn.
- `mcp.log` — stdout+stderr tail of the detached server.

## Architecture

Single binary, layered packages. Don't bypass layers:

1. **`cmd/droids-mem/`** — cobra subcommands. CLI ops (`save`, `search`, `context`, `list`, `get`, `schema`, `doctor`) + bridge ops (`serve`, `ensure-server`). Each `cmd_*.go` parses flags, delegates, emits JSON via `output.go`. No business logic here.
2. **`internal/mcpserver/`** — MCP bridge server (mark3labs/mcp-go, Streamable HTTP). `Run(ctx, cfg, store)` wires four tools (`mem_save`, `mem_search`, `mem_context`, `mem_get`), bearer auth at the `net/http` layer, SIGTERM graceful shutdown. Called by `cmd_serve.go`. Hides operator commands (`list`, `schema`, `doctor`) — those stay CLI-only. See `docs/adr/0003-mcp-bridge-for-agentspan.md`.
3. **`internal/store/`** — all business logic. Save validates + normalizes + fingerprints + dedupes (2 layers) + inserts. Search runs FTS5. Context assembles priority slots. **Shared by every entry point** — fix bugs once, both transports get them.
4. **`internal/db/`** — connection open + DDL only. Schema in `schema.go` (raw SQL string `ddl`).
5. **`internal/state/`** — owns `~/.droids-mem/` (token, pid, log). `LoadOrCreateToken()` is the canonical bearer-token resolver.

### Data model invariants

- `memories` is source of truth. `memories_fts` is search index only — never query for filtering or joins on data; only for `MATCH` + `rank`.
- FTS sync is via 3 triggers (AI/AD/AU). Direct writes to `memories_fts` (other than `'delete'` command rows) are bugs.
- FTS returns `rowid`; bridge to `memories.id` (TEXT, ULID with `mem_`/`sess_` prefix). Callers never see rowid.
- `tags` stored as space-delimited string (NOT JSON) — FTS5 tokenizes on whitespace.
- `updated_at = created_at` set in code on insert (`save.go`). Only mutated on `force=true` overwrite. Never `DEFAULT 0` (breaks recency tiebreaks). CHECK constraint guards `updated_at >= created_at` at DB layer.

### Dedupe (`save`)

Two layers, both must pass before insert:
1. **Fingerprint** — SHA-256 of normalized (lowercase → trim → collapse ws → strip punct → sort words) `title+learned` concatenated with `task_type`+`kind`. Exact match → skip (or overwrite if `force=true`).
2. **BM25 pre-save** — FTS query using new memory's title+learned. If top result `rank < -15.0` (constant, column weights `bm25(memories_fts, 3, 1, 2, 1)`), treat as near-duplicate → skip.

### Context bundle (`context`)

4 priority slots, dedup by `id` across slots, cap by `--limit` (default 8):
- 1× latest `session_summary` for `task_type` (recency)
- ≤3× `error_resolution` (FTS rank on `--query` or tokenized `task_type`)
- ≤2× `user_rule` (recency)
- ≤2× `task_pattern` (FTS rank)

### Session retention

On every `session_summary` save: count existing for `task_type`, delete oldest if > 5. Bounds growth automatically.

## CLI contract

Strict. Agents depend on it.

- All output: JSON to stdout. Errors: JSON to stderr.
- Exit codes: `0` success, `1` runtime, `2` usage, `3` not found, `5` conflict/duplicate, `10` dry-run pass.
- All flags long-form (`--task-type`, `--session-id`, `--dry-run`, `--no-interactive`). No short aliases in V1.
- TTY-aware via `mattn/go-isatty`: non-TTY forces JSON, no colors, no prompts, never blocks on input.
- `--dry-run` on `save` returns structured JSON of what would happen; exit `10`.
- `save` accepts optional `--session-id`; if omitted, binary generates ULID and returns it. Agent should capture and reuse for grouping.
- Error envelope: `{status, code, field?, message, input?, retryable, suggestion}`.
- `schema` subcommand returns parameter definitions as JSON — for agent introspection.

## MCP contract

`droids-mem serve` (powered by `internal/mcpserver`) exposes 4 tools over JSON-RPC. Same business logic as the CLI, different transport.

- Tools: `mem_save`, `mem_search`, `mem_context`, `mem_get`. `mem_list`, `mem_schema`, `mem_doctor` are intentionally not exposed — operator commands stay CLI-only.
- `mem_context` returns `{ "session_id": "sess_...", "context": <ContextResponse> }`. The server **mints** the `session_id` on first call and returns it; the agent stores it in its own durable state and threads it through subsequent `mem_save` calls. The server is otherwise stateless — no per-connection session map (would break agentspan's pause-and-resume-on-different-worker semantics).
- Auth: `Authorization: Bearer <DROIDS_MEM_MCP_TOKEN>` on every request to `/mcp`. Constant-time compare. `/healthz` is exempt.
- Validation errors from `internal/store` (`*store.ValidationError`) become `{error, field, message}` MCP tool errors; runtime errors propagate as plain text.
- Shutdown: SIGINT/SIGTERM trigger `http.Server.Shutdown` with a 10 s grace; deferred `db.Close` runs after Shutdown returns so no writer txn is killed mid-flight.

## Dependencies (locked)

- `modernc.org/sqlite` — pure Go SQLite, FTS5 supported, no CGO. Do not swap for `mattn/go-sqlite3`.
- `github.com/oklog/ulid/v2` — IDs.
- `github.com/spf13/cobra` — CLI.
- `github.com/mark3labs/mcp-go` — MCP server SDK (Streamable HTTP + stdio). Used only by `internal/mcpserver`.

## Reference docs

- `droids-mem/files/Droids-mem-PRD.md` — full product spec, data model, response shapes.
- `droids-mem/M0-decisions.md` — locked pre-impl decisions (transport, lib choices, thresholds). Read before changing any design assumption.
- `droids-mem/files/CLI-GUIDE.md` + `CHECKLIST.md` — CLI design rules the binary follows.
- `droids-mem/CONTEXT.md` — domain language and term aliases.
- `droids-mem/docs/adr/` — accepted ADRs. `0003-mcp-bridge-for-agentspan.md` covers the MCP transport, bearer auth, and session-ownership decisions.
- `droids-mem/Future.md` — deferred / post-V1 ideas.
