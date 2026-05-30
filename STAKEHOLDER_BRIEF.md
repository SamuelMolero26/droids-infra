# droids-mem: Stakeholder & Recruiter Brief

## Short Pitch (Non-Technical & LinkedIn)

**droids-mem** solves the statelessness problem in AI agents. It's a local-first persistent memory layer that gives LLM-based agents durable, structured knowledge across runs — memories of bugs fixed, patterns learned, user rules, and session summaries that survive agent restart and get injected as focused context at startup. Built as a single Go binary with a SQLite backend and exposed via JSON-RPC (MCP), it powers zero-config integration: just ping the server, save a memory, retrieve your focused context bundle. Designed for long-running agent workflows where learning compounds across work sessions.

---

## Technical Deep-Dive (For Recruiters & Technical Interviewers)

### System Architecture & Scope
- **Single Go binary** (`droids-mem`) with layered package design: CLI layer (cobra subcommands), MCP bridge, business logic (store), database/state management. Clean separation of concerns — fix bugs once, both transports benefit.
- **Four locked memory kinds** (session_summary, task_pattern, error_resolution, user_rule) — deliberate constraint to keep schema stable and dedupe deterministic. Post-V1 extensibility deferred via ADR.
- **Two transport entry points**: CLI (strict JSON stdout/stderr contract, typed exit codes 0/1/2/3/5/10) and MCP server (mark3labs/mcp-go, Streamable HTTP, bearer auth, stateless session minting). Interchangeable thanks to shared business logic layer.

### Data Persistence & Search
- **Pure Go SQLite** (modernc.org/sqlite) — no CGO, single binary shipping. WAL mode enabled for concurrent read/write; foreign_keys=ON enforced at schema layer. Schema applied idempotently on every `Open()` via `IF NOT EXISTS`.
- **FTS5 with trigram tokenizer** for substring search across memory content (title, learned field, tags). Dual-table design: `memories` is source of truth, `memories_fts` is search index only. FTS sync via 3 INSERT/UPDATE/DELETE triggers — direct writes to FTS are treated as bugs.
- **Two-layer dedupe on save**: (1) SHA-256 fingerprint of normalized title+learned+task_type — exact match blocks insert or overwrites if `--force=true`, (2) BM25 pre-save against FTS top-20 results with Jaccard ≥ 0.85 similarity threshold. Both checks execute in a single `BEGIN IMMEDIATE` transaction to close race windows in concurrent save scenarios.

### Context Assembly & Session Management
- **Context bundle tier model** (ADR 0002): "always" tier delivers full session summary + all user rules (uncapped); "browse" tier surfaces up to 2 task patterns + 3 error resolutions as title + 120-rune snippet, rank-sorted by BM25. Dedup across tiers by memory ID, capped by `--limit` (default 8). Assembled in a `BEGIN DEFERRED` snapshot for consistency.
- **Stateless session ownership**: MCP server mints a session_id (ULID with `sess_` prefix) on first `mem_context` call; agent threads this session_id through subsequent `mem_save` calls for grouping. Server maintains no per-connection state — survives pause/resume across different workers (agentspan compatibility). Separate session_summary retention loop: on every save, count existing summaries for that task_type; delete oldest if > 5.
- **Consumer pattern (ADR 0004)**: Only Root agent calls droids-mem. Sub-agents receive NO memory tools — they consume context bundles injected by Root into their system prompts. Root performs a Rollup at session end: N `mem_save` calls composing cross-run learnings. Keeps write traffic bounded, dedupe clean, and replay safe across restarts.

### Operational & DevEx
- **Zero-config startup** via `ensure-server` subcommand: pings `/healthz` endpoint; if server down, re-execs itself as a detached process (Setsid) with stdout/stderr teed to `~/.droids-mem/mcp.log`. Single command, zero env vars required (all optional, sensible defaults in `internal/state`).
- **TTY-aware CLI** (mattn/go-isatty): non-TTY mode forces JSON output, strips colors, skips interactive prompts — ideal for agent/automation orchestration. Strict contract: all output JSON, errors structured with field/message/retryable/suggestion envelope, exit codes typed.
- **Comprehensive E2E testing**: Two test suites — one drives the CLI binary end-to-end (tests full command lifecycle), one spawns `droids-mem serve` on ephemeral port and drives it via JSON-RPC (tests auth, tool surface, session minting, dedupe, graceful SIGTERM shutdown). Tests isolate DB and state dir per run to avoid cross-test pollution.

### Engineering Decisions & Ambition
- **No observation kind** (ADR 0004 documents why): agents don't save arbitrary observations. Frozen memory kinds force discipline — patterns, errors, rules, summaries only. Trades flexibility for determinism and long-term schema stability.
- **Fingerprint-based dedupe + BM25 similarity**: acknowledges that "same learning expressed differently" is a valid near-duplicate. Tuned constant (-15.0 BM25 threshold) refined empirically; weights (3, 1, 2, 1 for title, learned, tags, type) reflect importance. Shows systems thinking: what looks identical in one domain (fingerprint) might need soft matching in another (semantic similarity).
- **Trigram FTS + substring search**: chose substring over whole-word matching because error messages and code snippets benefit from partial matching (e.g., "TypeError" matches "Type" + "Error" prefix patterns). Trigram tokenizer bakes this in at index time.
- **Graceful degradation**: bearer auth on MCP is optional (env var or auto-generated token in state dir); server startup doesn't require external services; database schema self-initializes. Designed for embedded, offline-first agent deployments.

### What This Demonstrates
- **Go systems design**: layered architecture, connection pooling (SQLite WAL), goroutine shutdown with context, idempotent schema application, structured error handling with typed exit codes.
- **Database systems**: FTS5 configuration + tuning, trigger-based index sync, transaction isolation levels (IMMEDIATE vs DEFERRED), race-window closure via atomicity.
- **AI systems thinking**: understands agent statefulness problem, designs for long-horizon workflows, composes learnings across runs, balances dedupe determinism vs. flexibility in soft-matching thresholds.
- **API design discipline**: MCP + CLI contract design, stateless session minting, operational concerns (TTY awareness, graceful shutdown, token bootstrap), E2E testing that validates both transports.

