# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Scope

This file covers the `droids-agents/` Python package. The sibling `droids-mem/` Go service has its own rules in `droid-infra/CLAUDE.md`. Treat `droids-agents/` as the working directory for everything below.

## Build / test / run

uv-managed, Python 3.12+. Console script registered as `droids-agents`.

```
uv sync --extra dev                              # install incl. dev tools
uv run droids-agents run "<prompt>"              # execute an Execution
uv run droids-agents tui                         # Textual dashboard
uv run droids-agents auth gmail                  # one-time OAuth desktop flow
uv run droids-agents doctor                      # pre-flight checks
uv run pytest                                    # unit tests only (default)
uv run pytest -m integration                     # integration: spawns real mcp + agentspan
uv run pytest tests/unit/test_slicing.py -k name # single test
uv run ruff check src tests                      # lint
uv run ruff format src tests                     # format
```

Tests live in `tests/unit/`. Integration tests are gated by `@pytest.mark.integration` (per `pyproject.toml`); they spawn ephemeral-port `droids-mem serve` + `agentspan` and are slow. `asyncio_mode = "auto"`.

## Runtime env

`.env` resolution order (later wins): `~/.droids-agents/.env`, then `./.env`. Loaded by `config._load_env_files`.

Required:
- `ANTHROPIC_API_KEY`
- `DROIDS_MEM_MCP_TOKEN` — bearer for `droids-mem serve`
- `DROIDS_MEM_MCP_URL` — default `http://localhost:7777/mcp`
- `AGENTSPAN_URL` — default `http://localhost:6767`
- `GOOGLE_CREDENTIALS_JSON`, `GOOGLE_TOKEN_JSON` — Gmail OAuth files

Optional:
- `DROIDS_AGENTS_LOG_DIR` — default `~/.droids-agents/logs/`
- `DROIDS_AGENTS_EMAIL_ALLOWLIST` — comma-separated domains. Empty → every recipient pauses HITL.
- `DROIDS_NAMES_FILE` — override for `droid-infra/droids-name.yml`

CLI exit codes: `0` ok, `1` runtime, `2` usage, `3` dep unreachable, `4` HITL pause, `5` cost cap hit, `10` dry-run pass. All errors emit JSON envelope on stderr in `--json` mode.

## Architecture

Single binary, layered Python packages. Build sequence and design locks live in `V1-droids-agents-plan.md`. Domain language in `CONTEXT.md` is authoritative — read it before renaming anything.

Layers (do not skip):

1. **`src/cli.py`** — click group (`main`) with subcommands `run`, `tui` (Typer-mounted), `auth gmail`, `doctor`. Flags only; no business logic. Orchestrates: load Settings → classify → fetch Bundle → build Root → connect runtime → start execution → render.
2. **`src/router.py`** — pre-compile classification. `classify_prompt` and `plan_mixed_steps` are plain Anthropic SDK calls on `claude-haiku-4-5` BEFORE agentspan compiles the Root. agentspan workflows are static, so dynamic `mixed` SEQUENTIAL must be planned in Python first. `build_root` picks a stable name per shape (`root_single_research`, `root_mixed_research_docs`) so agentspan's compile cache hits.
3. **`src/agents/`** — one file per Subteam (`research`, `docs`, `form`, `messaging`). Each exports a `*_team(pool, *, slice_lines, ...)` factory returning an `Agent`. Strategies: research=PARALLEL, docs=SWARM (handoff via `OnTextMention`, `max_turns=10`), form=HANDOFF, messaging=HANDOFF. Specialists run `claude-sonnet-4-6`.
4. **`src/guardrails/`** — `Position.INPUT` / `Position.OUTPUT` validation. `OnFail` modes layered: RETRY first, then HUMAN. Files mirror Subteam names + `router.py` (jailbreak) + `docs.py` (citations). HITL has two distinct mechanisms — see below.
5. **`src/tools/`** — `@agentspan.agents.tool` wrappers. `mem.py` exposes both the `mem_tools(settings)` MCP attachment AND a direct `fetch_mem_context` httpx call (the CLI uses the latter pre-compile). `playwright.py` owns a module-level `_browsers: dict[exec_id, BrowserContext]` registry with an asyncio.Lock; CLI registers teardown via runtime callbacks. `gmail.py`'s `_service()` never opens a browser — it loads/refreshes the token and raises `GmailAuthError` for missing/malformed/revoked cases (`auth gmail` is the only OAuth entrypoint). `files.read_doc` caps at 50k chars/doc, branches on `.pdf/.md/.txt`.
6. **`src/schemas.py`** — Pydantic models for every Sub-agent output and droids-mem payload. Sub-agents emit typed outputs; the Rollup composes them deterministically (no LLM JSON parsing).
7. **`src/slicing.py`** — `slice_for(role, bundle, prompt)` is rule-based (no LLM). Slicing is per-Role, not per-Sub-agent; CLI builds the slice map once via `_build_slice_map`.
8. **`src/naming.py`** — `NamePool` claims unique Droid names per Execution. `agentspan.Agent.name` stays stable (e.g. `competitor_0`) for the compile cache; Droid names live in `metadata={"droid_name": ...}` and surface only in CLI/logs.

### Memory broker pattern (CRITICAL, ADR 0004)

The **Root agent is the sole reader/writer of droids-mem per Execution.** Sub-agents are pure compute and never call mem tools. Implementation reality (V1 simplification from the PRD's `memory_loader` Agent):

- The **CLI** calls `fetch_mem_context` directly via JSON-RPC BEFORE compiling Root. The minted `session_id` is captured and threaded through every subsequent `mem_save` call.
- Subteam factories receive `slice_lines` baked into their `instructions` closure at build time. They cannot mutate or refetch.
- Only the **Rollup agent** has `mem_tools(settings)` attached. It writes one `kind=session_summary` record per Execution.

Why: agentspan compiles a static `WorkflowDef` up front; an Agent-level `memory_loader` would block static composition. Also, semantic purity — schema stays at 4 kinds (no `observation`).

### Wire-format key discipline

droids-mem's MCP envelope uses `session_id` (not `sess_id`). `sess_id` is shorthand in prose ONLY. Use the constant `MEM_SESSION_KEY = "session_id"` from `tools/mem.py` for every payload read/write. Mixing keys silently breaks `mem_context` chaining.

### HITL — two stacked mechanisms

Both surface on the agentspan UI (`localhost:6767`). Use both together where defense-in-depth matters (e.g. `gmail_send`):

1. **Approval-required tool** — `@tool(approval_required=True)`. Deterministic, every invocation pauses. Currently: `gmail_send`, `web_submit`.
2. **Guardrail with `OnFail.HUMAN`** — Conditional, pauses only when validation fails. Stacked AFTER RETRY-mode guardrails (agentspan runs in order; first failure wins). Examples: `pii_in_draft`, `recipient_allowlist`, `citations_resolve`.

### Pre-session logging buffer

`session_id` is unknown until `fetch_mem_context` returns. `logging.py` owns a thread-safe `queue.Queue` buffering up to 10k records; `bind_session(session_id)` (called exactly once from CLI) flushes to `<log_dir>/<session_id>.jsonl` and switches structlog's processor chain to direct-file mode. Overflow fails loud — that means `session_id` never resolved.

### Cost cap

`pricing.usd_to_max_total_tokens(budget_usd)` uses a blended sonnet rate (30% prompt @ $3/Mtok + 70% completion @ $15/Mtok = $11.4/Mtok) — conservative for haiku-heavy or cache-heavy runs. agentspan Python's `TokenUsageTermination` only exposes `max_total_tokens`; the split prompt/completion params are TypeScript-only. `token_usage` aggregates tree-wide, so attaching termination to Root captures everything.

### Browser lifecycle

`tools/playwright._browsers` is keyed by `exec_id` (from agentspan `ToolContext.execution_id`). Parallel Sub-agents in the same Execution share one BrowserContext; different Executions are isolated. Teardown via `on_complete` / `on_fail` runtime callbacks; a daemon sweep closes contexts older than 1 hour as leak insurance. Worker restart → context recreates lazily; read-only actions replay safely, `web_submit` re-prompts HITL.

## Dependencies (locked)

- `agentspan>=0.1.0` — durable orchestration. Use `mcp_tool`, `Agent`, `Guardrail`, `Strategy`, `TokenUsageTermination`. Workflows compile statically — no dynamic topology inside an Agent.
- `anthropic>=0.40.0` — direct SDK for classifier + mixed_planner only. Specialists are LLM-routed by agentspan.
- `playwright` (Python) — persistent BrowserContext per Execution.
- `google-api-python-client` + `google-auth-oauthlib` — Gmail. OAuth flow only in `droids-agents auth gmail`.
- `pypdf` — PDF text extract in `read_doc`.
- `click` (main CLI) + `typer` (subcommands by convention — mounted onto the click group via `typer.main.get_command`).
- `structlog`, `rich`, `textual`, `pydantic`, `pyyaml`, `python-dotenv`, `httpx`.

Ruff: line-length 100, select `E F I B UP ASYNC`, ignore `E501`.

## Reference docs

- `files/Droids-agents-PRD.md` — product spec, topology, allowed scopes.
- `V1-droids-agents-plan.md` — locked design decisions and build phases. Overrides the PRD where conflicting.
- `V1-impact-evaluation.md` — risk/impact analysis of plan deviations.
- `CONTEXT.md` — domain language (Execution, Subteam, Memory broker, etc.). Required reading before renaming or refactoring.
- `FUTURE-Agents.TODO` — deferred / post-V1 ideas.
- `../droids-mem/docs/adr/0004-agent-broker-pattern.md` — the consumer-side decision that the Root agent is sole mem writer.
