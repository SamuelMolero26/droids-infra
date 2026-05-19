# Droids-agents PRD (V1, local-first)

## Context

`droids-mem` provides agents with persistent memory (Go binary + MCP bridge at `cmd/droids-mem-mcp`). The next layer needs a durable, resilient multi-agent runtime so those agents can actually perform business-intelligence work. V1 stays **local-first**: orchestration, state, and tools run on the user's machine; only LLM API calls leave the box. The system is the substrate for: competitor research, document synthesis, form submission, and email messaging — with HITL gates on anything irreversible.

## Goals (V1)

1. One CLI (`droids-agents <prompt>`) spins up a router → subteam multi-agent pipeline.
2. Durable runtime: crash/restart recovery, pause-for-human-approval, replayable execution log.
3. Native integration with `droids-mem` over MCP (shared memory across subteams via one session).
4. Concrete capability: scan N competitors in parallel, synthesize docs into findings, submit a form OR draft+send an email — all with explicit human approval before side effects.

## Non-Goals (V1)

- Internal SQL/CSV data analysis (deferred to V2).
- Slack / SMS channels (Gmail only V1).
- URL ingestion for docs (explicit path only V1).
- Custom approval UI (use agentspan built-in :6767).
- Distributed / cloud deployment (single-machine local).
- Local LLM (cloud APIs only V1).

## Stack

| Layer            | Choice                                       |
|------------------|----------------------------------------------|
| Orchestration    | `agentspan` (Python SDK + server)            |
| State backend    | agentspan default SQLite (`agent-runtime.db`)|
| LLM provider     | OpenAI / Anthropic via cloud APIs            |
| Memory           | `droids-mem-mcp` (HTTP, bearer auth, :7777)  |
| Browser          | Playwright via Python lib (per-Execution BrowserContext) |
| Email            | Gmail MCP                                    |
| Secrets          | `.env` + python-dotenv                       |
| CLI              | Python `click` (or `typer`)                  |
| Logging          | `structlog` JSON → `~/.droids-agents/logs/`  |
| HITL surface     | agentspan UI on `localhost:6767`             |

## Topology

CLI classifies the prompt (plain Python haiku call) before any agentspan compile. The Root is one of N pre-shaped `SEQUENTIAL` topologies — agentspan compiles each shape once (cache keyed on `Agent.name`).

```
[CLI: classify_prompt → label → (plan_mixed_steps if label == "mixed")]
        │
        ▼
Root = SEQUENTIAL([
   memory_loader        (haiku, tools=[mem_context], output_type=MemoryLoaderResult)
   <chosen Subteam(s)>  (one of the 4 single Subteams, or the planned sequence for mixed)
   rollup               (haiku, tools=[mem_save], output_type=RollupResult)
])

Subteams:
├─ research_team   Strategy.PARALLEL  (1 leaf competitor agent per competitor; guardrails on each leaf)
├─ doc_team        Strategy.SWARM     (extractor ↔ synthesizer; max_turns=10; guardrails on synthesizer leaf)
├─ form_team       Strategy.HANDOFF   (planner + executor; web_submit gated approval_required=True)
└─ messaging_team  Strategy.HANDOFF   (drafter + sender; gmail_send gated approval_required=True)
```

- **research_team (parallel)** = N competitor leaf agents run concurrently; each emits one `CompetitorFinding` (Pydantic); parent aggregates list.
- **doc_team (swarm)** = `OnTextMention` handoffs between extractor / synthesizer; bounded by `max_turns=10`; synthesizer emits `DocSynthesis`.
- **form_team / messaging_team (handoff)** = parent's LLM picks one specialist; the irreversible tool (`web_submit`, `gmail_send`) is decorated `@tool(approval_required=True)` → durable pause until UI approval.

## Repo layout

```
droid-infra/
├─ droids-mem/                       (Go — existing)
└─ droids-agents/                    (Python — new, V1)
   ├─ pyproject.toml                 (uv-managed)
   ├─ src/droids_agents/
   │  ├─ cli.py                      (`droids-agents <prompt>` entry)
   │  ├─ runtime.py                  (AgentRuntime + workers bootstrap)
   │  ├─ root.py                     (router + subteam wiring)
   │  ├─ agents/
   │  │  ├─ research.py              (parallel competitor agents)
   │  │  ├─ docs.py                  (swarm extractor/synth)
   │  │  ├─ form.py                  (handoff + approval-gated submit)
   │  │  └─ messaging.py             (handoff + approval-gated send_email)
   │  ├─ tools/
   │  │  ├─ mem.py                   (droids-mem MCP client wrapper)
   │  │  ├─ playwright.py            (Playwright MCP client wrapper)
   │  │  ├─ gmail.py                 (Gmail MCP client wrapper)
   │  │  └─ files.py                 (read_local_doc, parse_pdf)
   │  ├─ config.py                   (.env loader, paths)
   │  └─ logging.py                  (structlog setup)
   ├─ tests/
   │  ├─ test_router.py
   │  ├─ test_research_parallel.py
   │  ├─ test_hitl_gate.py
   │  └─ e2e/                        (full CLI E2E w/ mocked LLM)
   └─ README.md
```

## Memory integration

**Session lifecycle**

- CLI runs the classifier (plain Python haiku call; see Stack) on the prompt; `task_type` is mapped from the resulting label (or taken from `--task-type` override).
- Root is compiled as `SEQUENTIAL([memory_loader, <chosen Subteam(s)>, rollup])`. The `memory_loader` step calls `mem_context(task_type=...)` as its first action. droids-mem returns `{session_id, context: ContextResponse}` — `session_id` is read from the MCP response envelope's top-level field.
- The Root threads `session_id` through every downstream step; only the `rollup` step writes back to droids-mem. Sub-agents never see mem tools (broker pattern).

**Broker-only save pattern (locked)**

The Root agent is the sole reader and writer of droids-mem for an Execution. Sub-agents are pure compute and never call mem tools directly. Granular in-flight observations live in agentspan's durable execution log; only curated finals reach droids-mem.

- **Root agent** (Memory broker):
  - Reads `mem_context(task_type=...)` once at Execution start; mints `sess_id`.
  - Produces per-role Slices from the Bundle and injects them into Sub-agent system prompts.
  - On completion, writes Rollup: `mem_save(kind="session_summary", sess_id=X, ...)`.
  - Optionally writes `kind="task_pattern"` for reusable recipes and `kind="error_resolution"` for failure modes worth recalling.
  - Optionally writes `kind="user_rule"` if the prompt or HITL response explicitly states a durable rule.
- **Sub-agents**: no MCP creds, no `sess_id` threading, no `mem_save` calls. Return structured values that the Root aggregates.

droids-mem stays at 4 kinds — `session_summary`, `error_resolution`, `user_rule`, `task_pattern`. No `observation` kind is added.

**Failure resilience**: agentspan durable execution log re-runs uncompleted steps after worker restart. Sub-agent returns survive in agentspan SQLite even if the Root crashes before Rollup. On replay, Rollup fires from re-aggregated Sub-agent state. droids-mem fingerprint + BM25 dedupe makes Rollup replay safe for deterministic content; non-deterministic Rollup text is mitigated by normalization in `save.go` (lowercase → trim → collapse ws → strip punct → sort words) before fingerprinting.

## CLI contract

Subcommands:

```
droids-agents run <prompt>             # default; bare `droids-agents <prompt>` aliases to `run`
  [--docs <path>[,<path>...]]          # explicit doc paths for doc_team
  [--task-type <str>]                  # override router classification
  [--session-id <sess_...>]            # reuse existing session_id (new writes grouped under it; not pause-resume)
  [--max-turns <int>]                  # default 20, --no-caps to disable
  [--max-cost-usd <float>]             # default 2.0, --no-caps to disable
  [--no-caps]                          # disable both caps (explicit opt-in)
  [--dry-run]                          # run pipeline end-to-end but skip irreversible tools + droids-mem writes; emit preview, exit 10
  [--json]                             # JSON event stream to stdout

droids-agents auth gmail               # one-time interactive OAuth desktop flow → writes token
droids-agents doctor                   # pre-flight checks (env vars, mem-mcp, agentspan, gmail token, playwright)
```

`auth gmail` is the ONLY path that opens a browser. Agent workers never trigger OAuth — they only refresh existing tokens. `doctor` exits 0 on green, 1 on any failure, emits JSON list of pass/fail entries.

Exit codes mirror `droids-mem`: `0` success, `1` runtime, `2` usage, `3` not found, `5` cap exceeded / rejected, `10` dry-run pass.

## HITL gates

Tools that mutate external state are decorated:

```python
@tool(approval_required=True)
def submit_form(url: str, fields: dict) -> dict: ...

@tool(approval_required=True)
def send_email(to: str, subject: str, body: str) -> dict: ...
```

- Execution pauses (inserts `WaitTask`) and persists in agentspan SQLite.
- **Two HITL surfaces, identical info on both:**
  1. **agentspan UI** at `http://localhost:6767` — execution card shows Droid name (`C-3PO: [Email-Sender]`), tool name, and proposed args. Approve / reject / respond-with-edit (`runtime.respond(id, {edited_output: ...})`) for body tweaks.
  2. **droids-agents CLI stdout** — when streaming events (default and `--json`), a pause prints a clearly delimited block:
     ```
     [PAUSE] C-3PO: [Email-Sender] awaiting approval (sess_01HG…, exec_…)
       tool: gmail_send
       args: to=alice@example.com, subject="Findings summary", body=<200-char preview…>
       approve at: http://localhost:6767/executions/<exec_id>
     ```
     The CLI block lets the user disambiguate without opening the UI, and includes the direct URL to the right execution card.
- **V1 assumes single user.** Disambiguating concurrent HITL pauses for multi-user / shared-machine setups is deferred. Tool args (recipient, subject, form fields, URL) are inherently self-describing for the two HITL-gated tools (`gmail_send`, `web_submit`).
- Pause survives restart. No timeout V1 — stale pauses sit indefinitely; user picks them up whenever.

## Concurrency

- `agentspan server start` runs once (background process).
- Each `droids-agents` CLI invocation enqueues a new execution via `runtime.start(root_agent, prompt)`.
- Workers process in parallel; each top-level run gets own `sess_id`. droids-mem WAL handles concurrent writers (already validated in V1 hardening).
- Parallel sub-agents inside one run also share that one `sess_id` (hybrid save pattern).

## Caps + termination

- `MaxMessageTermination(max_messages=<max_turns>)` combined with `TokenUsageTermination(max_total_tokens=<derived>)` on the Root via `|` operator. Token cap is derived from `--max-cost-usd` via the Anthropic price table (sonnet worst-case, 30/70 prompt/completion blend → ~$11.4/Mtok effective). agentspan aggregates token usage tree-wide (verified per agentspan `skills.md` docs), so a Root-level cap covers all sub-agent LLM calls.
- On breach → execution terminates; Root's `rollup` step writes `kind="error_resolution"` capturing the cap event (cap exceeded, partial state); CLI exits `5`.
- `--no-caps` explicitly skips both (opt-in for long research runs).

## Observability

- agentspan UI: per-execution timeline, per-tool args/returns, error traces. Free.
- `structlog` JSON lines to `~/.droids-agents/logs/<sess_id>.jsonl` for offline analysis: full per-call cost ledger (model, prompt/completion tokens, USD), tool latency, guardrail decisions. Authoritative source for cost drill-down.
- Aggregate cost summary (`Cost: $X total / Y prompt + Z completion tokens / M messages`) is appended to the Rollup body so it's searchable via `mem_search` per-Session. droids-mem stays at 4 kinds — no `observation` kind is added.

## Critical files to reuse

- `droids-mem/cmd/droids-mem-mcp/` — bridge already exposes `mem_save / mem_search / mem_context / mem_get`. Don't duplicate logic.
- `droids-mem/CLAUDE.md` — read before any cross-binary work (CLI contract, exit codes, env vars).
- Playwright MCP skill at `~/.codex/skills/playwright/` — wrap, don't reimplement.
- agentspan default SQLite lives in the server's cwd. Pin the server to `~/.droids-agents/` (boot sequence below) so the DB lands at `~/.droids-agents/agent-runtime.db`, alongside `logs/`. CLI cwd is irrelevant — it connects via `AGENTSPAN_URL`.

## Verification (E2E)

1. **Boot**: `agentspan server start &` → :6767 reachable. `droids-mem-mcp` running on :7777 with valid `DROIDS_MEM_MCP_TOKEN`. `.env` populated.
2. **Smoke router**: `droids-agents "research Anthropic vs OpenAI pricing"` → classifier picks `research_team` → parallel agents complete → session summary in droids-mem (`droids-mem search --task-type competitor_research`).
3. **HITL gate**: `droids-agents "email alice@example.com a summary of our findings"` → execution pauses → :6767 UI shows pending tool `send_email` → approve via UI → email sent via Gmail MCP → `kind="session_summary"` saved with sess_id.
4. **Restart recovery**: kill agentspan worker mid-research → restart → execution resumes from durable log, partial observations preserved.
5. **Caps**: `droids-agents "..." --max-cost-usd 0.01` → cancels mid-execution, exit `5`, error_resolution memory written.
6. **Dry-run**: `droids-agents "..." --dry-run` → no irreversible tools invoked, plan JSON printed, exit `10`.
7. **Concurrent runs**: two `droids-agents` invocations in parallel → both complete, both get distinct sess_id, droids-mem rows correctly attributed.

Tests (`droids-agents/tests/`):
- Unit: router classification, session threading, cap enforcement.
- Integration: mocked-LLM swarm loop in doc_team, approval-required tool blocks then resumes.
- E2E: spawn agentspan server + droids-mem-mcp on ephemeral ports, run full CLI flow.

## Open items (V2+)

- Slack + SMS messaging channels.
- URL fetch for doc_team; watch-dir inbox.
- Internal data analysis subteam (SQL/CSV/pandas).
- Custom approval UI with email-draft editor + Slack-button approvals.
- Multi-machine deployment (agentspan distributed workers).
- OpenTelemetry / Langfuse traces.
