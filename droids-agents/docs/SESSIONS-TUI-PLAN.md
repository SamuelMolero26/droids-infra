# Sessions TUI — design (grilled)

## Goal

A TUI for droids-agents that (1) runs multiple live sessions with live stats +
interactive conversation, and (2) browses + searches previous sessions saved to
droids-mem. TUI only — no CLI subcommand.

## Locked decisions

| # | Decision | Choice |
|---|----------|--------|
| Surface | Where the feature lives | **TUI only** |
| Read path | How TUI reads droids-mem | **subprocess `droids-mem list`/`search`** (matches existing `ensure-server` pattern; keeps bridge agent-facing per ADR 0003) |
| Detail fetch | Per-session memories | **one `list --limit 100` + group by `session_id` client-side** (V1; `--session-id` filter deferred — see CLI.todo) |
| Scope | Features | **all 4** (browser, search, live session tabs, stats+conversation) |
| Concurrency | Multiple live sessions | **yes, small fixed cap (~3)**, session registry, **thread-per-session** |
| Live updates | Source of live stats/feed | **`runtime.stream()` → AgentStream** (events + `.send()` + HITL). Replaces blocking `run()` |
| Modules | Structure | **`sessions.py` + `memquery.py` + screen-split `tui.py`; `execution.py` unchanged** |
| Build order | Sequencing | **phased (A)** — see below |

## Constraints (verified)

- **agentspan server**: concurrent executions are native (durable runtime). No limit for a few sessions.
- **droids-mem**: stateless bridge; SQLite single-writer (`SetMaxOpenConns(1)`) + `BEGIN IMMEDIATE` + `busy_timeout=5000` → concurrent writes *serialize*, don't fail. `session_id` isolates each run. **No droids-mem changes needed for V1.**
- **droids-agents TUI**: the real constraint — today single-session (one `_RunState`, one worker thread, one `@work(exclusive=True)` poller, blocking `run()`). Must become a per-session registry.
- **Cost**: N concurrent sessions = N× LLM spend + N× token budget → the cap-3 guard.

## agentspan APIs in play

- `runtime.stream(root, prompt)` → `AgentStream`: iterable (yields events: messages, agent transitions, token_usage), has `.send(msg)` for multi-turn ("type to join the conversation"), surfaces HITL pending tool calls as events.
- `AgentResult.messages`, `.token_usage` (total/prompt/completion), `.sub_results` (per agent_name) → derive stats panel (Messages, Agents N/M, Turns, Cost via pricing.py, Status).

## Module layout

**`src/sessions.py`** (pure-ish, testable with a stub runtime)
- `SessionState` — per session: status, exec_id, messages, agents (name+role), token_usage→cost, turns
- `SessionRegistry` — cap-3 dict; `spawn(prompt)` → `execution.plan_execution` + `build_execution`, starts a thread iterating `runtime.stream(...)` ingesting events into the session's state; `send(sid, msg)`, `close(sid)`

**`src/memquery.py`** (testable, stub subprocess)
- `list_sessions()` — `droids-mem list --limit 100` → group by `session_id` → ordered sessions w/ their memories
- `search(query)` — `droids-mem search --query <q>`

**`src/tui.py`** (presentation only, screen-split)
- `SessionTabsScreen` — tab bar of open `SessionView`s (image 1)
- `SessionView` — conversation pane + Statistics panel + input box + footer (image 2)
- `SessionBrowserScreen` — `Ctrl+P` modal, saved sessions, `j/k`/`enter`/`esc` (image 3)
- `SearchScreen` — search modal (image 4)

**`src/execution.py`** — unchanged orchestration core (`plan_execution`/`build_execution`/`interpret_result`).

## Build phases (each independently shippable)

1. **[DONE]** Read-path: `memquery.py` + `SessionBrowserScreen` (Ctrl+P) + `SearchScreen` + `SessionDetailScreen`. Images 3+4. `ENABLE_COMMAND_PALETTE=False` to free Ctrl+P.
2. **[DONE]** Streaming single session: `sessions.py` (`SessionState` + `run_session` via `runtime.stream`) + `SessionView` (feed + Statistics panel + join input). Image 2. Replaced blocking `run()`. Live cost is best-effort (token_usage at end).
3. **[DONE]** Multi-session: `SessionRegistry` (cap 3, thread-per-session) + `SessionsScreen` (TabbedContent, Ctrl+N new / Ctrl+W close / Ctrl+P browse) + `SessionPane` (composable, polls one state) + `NewSessionModal`. Image 1. `SessionView` removed (superseded).

## Deferred polish
- Tab label is `S{n}: <prompt snippet>`; image 1 shows agent **name + role**. Agents are only known after the plan runs (async) — update the TabPane title from the snapshot once `agents_seen`/roles are populated.
- Live cost (`token_usage` only at stream end) — fetch `get_status` mid-run for live cost if wanted.

## Out of scope / deferred
- `droids-mem list --session-id` filter (CLI.todo) — V1 groups client-side.
- Concurrency > cap.
