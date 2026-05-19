# Implementation plan: droids-agents V1

## Context

PRD at `droid-infra/droids-agents/files/Droids-agents-PRD.md` defines the V1 scope: local-first multi-agent BI runtime (research / docs / form / messaging) consuming `droids-mem` via MCP, durable execution via agentspan, HITL on irreversible actions.

This plan is the concrete build sequence. Major design refinements from grilling (override PRD where conflicting):

- **droids-mem stays at 4 kinds** (no `observation` added). Schema purity preserved.
- **Parent-as-memory-broker**: only parent agents call droids-mem. Sub-agents are pure compute; results return through agentspan's durable execution log (which IS the granular store).
- **LLM lock**: Anthropic only V1. `claude-sonnet-4-6` for specialists, `claude-haiku-4-5` for router + slicer.
- **Cost cap** via agentspan native `TokenUsageTermination` (USD → tokens via Anthropic price table). No litellm.
- **Tools**: droids-mem via `agentspan.agents.mcp_tool` (auto-discovery); Playwright via Python `playwright` lib + persistent browser; Gmail via `google-api-python-client`.
- **Router labels**: 5 = `research | docs | form | messaging | mixed`. `mixed` → sequential pipeline across needed subteams.
- **Context slicing**: rule-based (by kind + subteam), no LLM call per slice.
- **HITL**: two layers stacked — `@tool(approval_required=True)` on `gmail_send` / `web_submit` (always pause); `Guardrail(on_fail=OnFail.HUMAN)` for conditional pauses (PII detected, missing citations, recipient not allowlisted). Both surface on :6767 UI.
- **Display names**: agents get a random Star Wars droid name from `droid-infra/droids-name.yml` for CLI/logging readability. Format: `C-3PO: [Researcher]`. Name reserved for the lifetime of the execution (no repeats across concurrent sub-agents in one run).
- **Tests**: agentspan `mock_run` + `MockEvent` + `expect()` for unit; `@pytest.mark.integration` gates ephemeral-port spawns of agentspan + droids-mem-mcp.

## Build order (5 phases)

### Phase 0 — droids-mem ADR + smoke

Document the consumer-side decisions inside droids-mem repo so future work doesn't drift.

- New file: `droid-infra/droids-mem/docs/adr/0004-agent-broker-pattern.md`
  - Status: Accepted. Records: 4-kind enum stays; sub-agents do not call mem_save directly; Root agent is sole writer per Execution; rationale (semantic purity + dedupe simplicity).
- No code change in droids-mem this phase.

### Phase 1 — droids-agents skeleton + config

- `droid-infra/droids-agents/pyproject.toml` (uv-managed, Python 3.12+).
  - Deps: `agentspan`, `anthropic`, `playwright`, `pypdf`, `google-api-python-client`, `google-auth-oauthlib`, `python-dotenv`, `click`, `structlog`.
  - Dev: `pytest`, `pytest-asyncio`, `pytest-mock`.
  - Console script: `droids-agents = droids_agents.cli:main`.
- `src/droids_agents/config.py`: load `.env` (cwd + `~/.droids-agents/.env`), expose `Settings` dataclass.
  - Required vars: `ANTHROPIC_API_KEY`, `DROIDS_MEM_MCP_TOKEN`, `DROIDS_MEM_MCP_URL` (default `http://localhost:7777/mcp`), `AGENTSPAN_URL` (default `http://localhost:6767`), `GOOGLE_CREDENTIALS_JSON` (path), `GOOGLE_TOKEN_JSON` (path).
  - Optional: `DROIDS_AGENTS_LOG_DIR` (default `~/.droids-agents/logs/`), `DROIDS_AGENTS_EMAIL_ALLOWLIST` (comma-separated domain list; empty → guardrail treats every recipient as not-allowlisted and pauses for HITL approval; required source for `recipient_allowlist` guardrail per S5 fix).
- `src/droids_agents/logging.py`: structlog JSON to `<log_dir>/<session_id>.jsonl` + stderr in dev.
  - **Pre-session buffer (M5)**: session_id is unknown until `memory_loader` emits it. Implementation: a `queue.Queue` (thread-safe) accumulates structlog records until session_id arrives, then a single-shot flush thread writes buffered records to the resolved log path and switches structlog's processor chain to direct-file mode for subsequent records. Buffer cap: 10k records (fail-loud on overflow — indicates session_id never resolved). The buffer lives in module-level state owned by `logging.py`; CLI's main entry calls `bind_session(session_id)` exactly once upon receiving the memory_loader result.
- `src/droids_agents/pricing.py`: Anthropic price table (per-Mtok input/output for sonnet-4-6, haiku-4-5, cached read). `usd_to_max_total_tokens(budget_usd) -> int` — agentspan Python `TokenUsageTermination` exposes only `max_total_tokens` (split prompt/completion params are TypeScript-only per context7 docs). Conversion uses sonnet worst-case with a blended 30% prompt @ $3/Mtok + 70% completion @ $15/Mtok = $11.4/Mtok effective: `max_total_tokens = int((budget_usd / 11.4) * 1e6)`. Conservative — overestimates cost for haiku-heavy or cache-heavy executions. Docstring notes: refine to a per-call USD callback (post-V1) if real-world drift > 30%.
- **Verified token aggregation**: agentspan's `result.token_usage` aggregates across the full execution tree including sub-agents (per context7 docs / `docs/python-sdk/skills.md`). `TokenUsageTermination` attached to Root therefore sees tree-wide usage. Verify in integration test 8.
- `src/droids_agents/naming.py`:
  - Loads `droid-infra/droids-name.yml` (resolved via repo root or `DROIDS_NAMES_FILE` env var).
  - `NamePool`: thread-safe `claim() -> str` / `release(name)` ensuring no duplicate name **within one Execution**. Replenishes from yaml; if pool exhausted, suffixes with numeric counter (`C-3PO-2`).
  - **Scope**: per-Execution only. Two concurrent Executions may both claim `C-3PO` — this is acceptable because Droid names are cosmetic; sess_id + exec_id are the authoritative identifiers shown on UI cards and CLI stdout (see Q12 disambiguation lock + start-line below).
  - `agent_display(name, role) -> str` returns `"{name}: [{role}]"` (e.g. `C-3PO: [Researcher]`).
  - Role mapping: `competitor_*` → `Researcher`, `extractor` → `Doc-Extractor`, `synthesizer` → `Doc-Synth`, `form_planner` → `Form-Planner`, `form_executor` → `Form-Executor`, `drafter` → `Email-Drafter`, `sender` → `Email-Sender`, `router_classifier` → `Router`.
  - On structlog event emit, augment log record with `agent_display` field so JSON lines + stdout banner show `C-3PO: [Researcher]`.

### Phase 2 — tools layer (no agents yet)

Each tool is a plain Python function decorated with `@agentspan.agents.tool`. Sub-agents get a subset.

- `src/droids_agents/tools/mem.py`:
  - Single helper `mem_tools(settings) -> list` returning `mcp_tool(server_url=..., headers={"Authorization": f"Bearer {token}"}, tool_names=["mem_save","mem_search","mem_context","mem_get"])`.
  - Parent agents attach these; subs never see them.
  - **Wire-format key discipline (M1)**: droids-mem `SaveRequest` JSON tag is `session_id` (not `sess_id`); `mem_context` MCP response envelope is `{session_id, context}` (per CLAUDE.md). All Python code that builds tool-call payloads or reads tool results MUST use the exact key `session_id`. `sess_id` is shorthand in prose only — never on the wire. Helper constants in `mem.py` enforce this (`MEM_SESSION_KEY = "session_id"`).
- `src/droids_agents/tools/playwright.py`:
  - **Lifecycle owner** (S2): agentspan does not expose arbitrary per-execution Python object storage. Implement an explicit module-level registry `_browsers: dict[str, BrowserContext]` keyed by `exec_id`, guarded by an `asyncio.Lock`. Each `@tool` wrapper takes `ToolContext` (injected by agentspan), reads `ctx.execution_id`, and lazily creates the `BrowserContext` if absent. The CLI registers an `on_complete` / `on_fail` callback via `runtime.start(...).on(...)` (or polls handle status) that calls `await _browsers.pop(exec_id).close()` to tear down. A daemon sweep thread closes contexts older than 1 hour as a leak safety net.
  - Parallel Sub-agents within one Execution share the context (same exec_id → same BrowserContext); different Executions get isolated contexts (no cross-run bleed). Worker restart → context recreated lazily on next tool call; read-only actions replay safely, write actions (`web_submit`) re-prompt HITL.
  - `@tool` async wrappers: `web_navigate(url)`, `web_extract_text(selector?)`, `web_screenshot(path)`, `web_fill(selector, value)`, `web_click(selector)`, `web_submit(form_selector)`.
  - `web_submit` decorated `approval_required=True`.
- `src/droids_agents/tools/gmail.py`:
  - `_service()` builder: loads token from `GOOGLE_TOKEN_JSON`, refreshes if expired-but-refreshable, returns Gmail API client. **Does NOT run OAuth flow** — agent workers must never open browsers. Three distinct error cases (M6):
    - Token file missing → `GmailAuthError("token file not found at $GOOGLE_TOKEN_JSON; run `droids-agents auth gmail` to create it")`.
    - Token file present but malformed → `GmailAuthError("token file at $GOOGLE_TOKEN_JSON is malformed; re-run `droids-agents auth gmail` to regenerate")`.
    - `google.auth.exceptions.RefreshError` (refresh token revoked at Google or expired beyond refresh) → `GmailAuthError("refresh token rejected by Google (likely revoked); re-run `droids-agents auth gmail` to grant fresh consent")`.
  - `@tool` wrappers: `gmail_draft(to, subject, body)`, `gmail_send(to, subject, body)` (latter `approval_required=True`), `gmail_list(query, max=10)`.
- `src/droids_agents/tools/files.py`:
  - `@tool` `read_doc(path)`: branches by extension — `.pdf` → pypdf text extract; `.md/.txt` → plain read; else error. Caps at 50k chars per doc.
  - Pure stateless tool — the `--docs` list itself is enforced + plumbed by the CLI (see below); `read_doc` just executes one path on demand.

### Phase 3a — guardrails

Two HITL mechanisms used together: `@tool(approval_required=True)` for deterministic per-invocation gates, `Guardrail(on_fail=OnFail.HUMAN)` for conditional gates on validation failures.

- `src/droids_agents/guardrails/router.py`:
  - `no_jailbreak` — `Position.INPUT`, `OnFail.RAISE`. Reject prompts containing "ignore previous instructions", role-override, etc. Pattern list.
- `src/droids_agents/guardrails/research.py` (layered — RETRY then HUMAN via two DIFFERENT functions; agentspan runs in order, first failure determines action):
  - `findings_structural` — `Position.OUTPUT`, `OnFail.RETRY` (max_retries=2). Cheap heuristic: each competitor finding has non-empty `summary` AND a `source_url` field.
  - `findings_quality` — `Position.OUTPUT`, `OnFail.HUMAN`. Stricter: `source_url` scheme is `http(s)`, `summary` length ≥ 50 chars, no "I couldn't find" / "as an AI" apology patterns. HITL fires only after RETRY produced output that still fails substantive checks.
- `src/droids_agents/guardrails/docs.py` (layered):
  - `citations_structural` — `Position.OUTPUT`, `OnFail.RETRY` (max_retries=2). Synthesizer output contains at least one `[source: <filename>]` marker per factual paragraph.
  - `citations_resolve` — `Position.OUTPUT`, `OnFail.HUMAN`. Every `cited_sources` basename appears in the `--docs` basename set for this Execution (no hallucinated sources). Basename is the citation key (short, human-readable, deterministic; CLI rejects duplicate basenames at parse time).
- `src/droids_agents/guardrails/messaging.py`:
  - `pii_in_draft` — `Position.OUTPUT`, `OnFail.HUMAN`. Regex SSN/credit-card/phone in email body → pause for redact/approve.
  - `recipient_allowlist` — `Position.INPUT` on `gmail_send` tool args, `OnFail.HUMAN`. Recipient domain must be in `Settings.email_allowlist` (loaded from `DROIDS_AGENTS_EMAIL_ALLOWLIST`) OR HITL-approved on this Execution. Empty allowlist = every send pauses for HITL (safe default).
  - `tone_length` — `Position.OUTPUT`, `OnFail.RETRY`. Body length cap (≤1000 words), profanity blocklist.
- `src/droids_agents/guardrails/form.py`:
  - `pii_in_form_fields` — `Position.INPUT` on `web_submit`, `OnFail.HUMAN`. SSN/credit-card values in field map require explicit approval.

All guardrails are pure functions returning `GuardrailResult(passed, message)` — fully unit-testable without LLM.

Attached per-agent:

```python
# Research guardrails attach to each LEAF competitor agent (S6 fix) — see research.py factory above.
# The research_team container has NO guardrails — single-object guardrail logic doesn't match an aggregated list.
research_team = Agent(
    name="research_team",
    strategy=Strategy.PARALLEL,
    agents=[competitor_agent(...) for ...],   # guardrails baked into each leaf
)
```

### Phase 3b — agents + router

**Sub-agent return contract**: every Sub-agent emits structured JSON via `response_format`. Root reads structured fields directly (no LLM parsing of prose) and composes the Rollup as template rendering over typed objects. Guardrails inspect the same structured fields (e.g., `findings_structural` checks `summary` / `source_url` on `CompetitorFinding`).

- `src/droids_agents/schemas.py`:
  - `TaskType = Literal["competitor_research", "doc_synthesis", "form_submission", "email_messaging"]` — fixed V1 vocab. Slicing rules, retention bounds, and `mem_context(task_type=...)` filters all match against these constants.
  - Static map from primary classifier label → `TaskType`:
    | Classifier label | TaskType                |
    |------------------|-------------------------|
    | research         | competitor_research     |
    | docs             | doc_synthesis           |
    | form             | form_submission         |
    | messaging        | email_messaging         |
    | mixed            | per-step (planner emits one classifier label per step → mapped via same table). Each step writes its own `session_summary` with its `TaskType`. |
  - `--task-type` CLI flag is optional and accepts only values from `TaskType` (validated at parse time); when omitted, Root derives it from the classifier — this is the normal path, no flag required for typical use.
  - **Sub-agent output Pydantic models** (passed as `output_type=Model` to agentspan `Agent(...)` — agentspan injects the schema into the system prompt and sets `jsonOutput=True`; do NOT use `response_format=`):
    - `CompetitorFinding(competitor: str, summary: str, source_url: str, notes: str | None)` — emitted by each parallel competitor agent.
    - `DocSynthesis(synthesis: str, cited_sources: list[str])` — emitted by doc synthesizer.
    - `FormPlan(url: str, fields: dict[str, str], rationale: str)` — emitted by form planner.
    - `FormSubmitResult(success: bool, response_url: str | None, error: str | None)` — emitted by form executor post-submit.
    - `EmailDraft(recipient: str, subject: str, body: str)` — emitted by drafter.
    - `EmailSendResult(message_id: str | None, error: str | None)` — emitted by sender.
  - **droids-mem write Pydantic models** — every `mem_save` requires `title`, `what`, `learned` all non-empty (per `droids-mem/internal/store/save.go:316-325`); fingerprint = SHA-256 of normalized `title + learned + task_type + kind` (excludes `what` by design, ADR 0001). Each kind's payload model:
    - `SessionSummary(task_type: TaskType, title: str, what: str, learned: str, tags: str = "")` — required Rollup output.
    - `TaskPattern(task_type: TaskType, title: str, what: str, learned: str, tags: str = "")` — reusable recipe (URL/selector/format).
    - `ErrorRecord(task_type: TaskType, title: str, what: str, learned: str, tags: str = "")` — failure mode worth recalling.
    - `UserRule(task_type: TaskType, title: str, what: str, learned: str, tags: str = "")` — durable preference from explicit user/HITL directive.
    - Field semantics: `title` = short label, `what` = context / what happened, `learned` = distilled reusable takeaway.
  - **Rollup composite**:
    - `RollupResult(summary: SessionSummary, new_patterns: list[TaskPattern] = [], new_errors: list[ErrorRecord] = [], new_rules: list[UserRule] = [])` with `max_items=3` on `new_patterns`/`new_errors`, `max_items=2` on `new_rules`.
  - **Memory broker envelope**:
    - `MemoryLoaderResult(session_id: str, task_type: TaskType, bundle: ContextResponse)` — `mem_context` MCP response is `{session_id, context}`; loader unwraps and re-packages; `session_id` is read from the MCP envelope's top-level field, NOT from inside `bundle`.

**Slice injection pattern**: Sub-agent factories take `focus_slice: str` and bake it into the agent's `instructions` via a `lambda` closure (agentspan evaluates callable instructions at run time). On agentspan replay, Root reconstructs Sub-agents — instructions are stable given the same Bundle. If new memories land between original run and replay, the Bundle (and therefore slices) may differ slightly → Sub-agents produce fresher output. Acceptable V1 behavior.

- `src/droids_agents/agents/research.py`:
  - **Agent name discipline**: agentspan `Agent.name` is "Used as the Conductor workflow name. Must start with a letter or underscore; may contain letters, digits, underscores, hyphens" (per agentspan docs). Compile cache is keyed on `name`. Therefore `Agent.name` must be a **stable identifier** like `competitor_0`, `competitor_1` — NOT a Droid name from the pool (some Star Wars names contain `-` which is allowed, but the pool is randomized → cache misses every Execution). Droid name is surfaced via `metadata={"droid_name": ...}` and shown in CLI/structlog output (Q11 lock); UI display may fall back to `name` if it doesn't surface metadata — acceptable.
  - Per-competitor sub-agent factory `competitor_agent(pool, focus_slice, competitor, index)`. Uses lambda **default-arg capture** to avoid Python's late-binding-in-loop trap (S1):
    ```python
    droid = pool.claim()
    return Agent(
        name=f"competitor_{index}",                      # stable, identifier-safe
        model="claude-sonnet-4-6",
        instructions=lambda s=focus_slice, c=competitor: f"Role: Researcher for {c}.\nPrior-run context:\n{s}\n\nTask: emit CompetitorFinding JSON.",
        tools=[web_navigate, web_extract_text],
        output_type=CompetitorFinding,
        metadata={"droid_name": droid, "role": "Researcher"},
        guardrails=[
            Guardrail(findings_structural, on_fail=OnFail.RETRY, max_retries=2),
            Guardrail(findings_quality, on_fail=OnFail.HUMAN),
        ],
    )
    ```
    Guardrails attach to the **leaf** competitor agent, not the `research_team` container (S6): each leaf produces one `CompetitorFinding`, guardrail logic inspects a single object — matches existing function signatures. agentspan aggregates leaf guardrail results at the parallel container per its documented Fork+aggregate pattern.
    No mem tools (broker pattern).
  - `research_team(parent_agent, competitors, slice_map)`: returns `Agent(name="research_team", strategy=Strategy.PARALLEL, agents=[competitor_agent(pool, slice_map[c], c, i) for i, c in enumerate(competitors)])`. No guardrails on the container.
- `src/droids_agents/agents/docs.py`:
  - `extractor_agent`: tools `[read_doc, web_extract_text]`.
  - `synthesizer_agent`: no tools, pure LLM. `output_type=DocSynthesis`. Leaf-attached guardrails: `Guardrail(citations_structural, on_fail=OnFail.RETRY, max_retries=2)`, `Guardrail(citations_resolve, on_fail=OnFail.HUMAN)`.
  - `doc_team`: swarm with `OnTextMention("HANDOFF_TO_SYNTH", target="synthesizer")` + reverse; `max_turns=10`.
  - **Cost budgeting note (M3)**: swarm `max_turns=10` × `citations_structural` RETRY (max_retries=2) → up to 3× baseline LLM cost on contested handoffs. Conservative bound: `~30` LLM calls per worst-case doc_team Execution. Surface this in `pricing.py`'s doctring; users running large doc_team prompts should set `--max-cost-usd` accordingly.
- `src/droids_agents/agents/form.py`:
  - `form_planner_agent`: navigates + fills, emits plan.
  - `form_executor_agent`: holds the `web_submit` approval-gated tool.
  - `form_team`: `strategy="handoff"`, parent picks between planner/executor.
- `src/droids_agents/agents/messaging.py`:
  - `drafter_agent`: writes email body via LLM, no external tools.
  - `sender_agent`: holds `gmail_send` approval-gated tool; tools also include `gmail_draft`.
  - `messaging_team`: `strategy="handoff"`.
- `src/droids_agents/router.py`:
  - Classifier and mixed_planner are **plain Python functions** (NOT agentspan Agents) — invoked by the CLI before `build_root` is called. Each function calls the Anthropic SDK directly on `claude-haiku-4-5` with constrained system prompts. They are not wired into the agentspan tool list or agent tree.
    ```python
    def classify_prompt(prompt: str) -> Literal["research","docs","form","messaging","mixed"]:
        # direct anthropic SDK call with strict instructions returning one token
        ...

    def plan_mixed_steps(prompt: str) -> list[Literal["research","docs","form","messaging"]]:
        # direct anthropic SDK call with JSON-schema-enforced response; max 4, no duplicates
        ...
    ```
  - **Why outside agentspan**: agentspan compiles agents into a static `WorkflowDef` up front (POST `/agent/compile`). A dynamic `mixed` SEQUENTIAL cannot be composed inside a running Agent. Running classifier in CLI before compile keeps the compiled topology static for each shape; `build_root` chooses one of N pre-shaped topologies, cache-keyed on a stable name like `root_single_research` / `root_mixed_research_docs_messaging`.
  - **`build_root(settings, prompt: str)` composition**: agentspan compiles agents up-front into a Conductor `WorkflowDef` (POST `/agent/compile`); the graph cannot be mutated mid-execution. Therefore `mixed` cannot be a dynamic SEQUENTIAL built inside `dispatcher`. Resolution:
    1. **`build_root` is called by the CLI** *after* the CLI runs `classify_prompt(prompt)` once (cheap haiku call, ~$0.0001). The CLI inspects the label:
       - Single label → builds `Root = SEQUENTIAL([memory_loader, <chosen Subteam>, rollup])`.
       - `mixed` → CLI also calls `mixed_planner(prompt)`, gets `steps`, builds `Root = SEQUENTIAL([memory_loader, *<each step's Subteam>, rollup])` with stable agent names per-permutation (e.g., `root_mixed_research_docs`) so the compile cache reuses across runs of the same shape.
    2. **`memory_loader`** — LLM-driven Agent (haiku). Tools: `[mem_context_mcp_tool]` only (NO classifier — classifier already ran in CLI; label/task_type is injected via `instructions` closure + `dependencies` or as part of the prompt). Instructions: "Call `mem_context(task_type=<known>, query=<prompt>)`. Return `MemoryLoaderResult`." `output_type=MemoryLoaderResult`. Reads `session_id` from MCP response envelope, not from inside `bundle`.
    3. **Subteam step(s)** — receive the prior step's output (`MemoryLoaderResult` for the first Subteam, prior Subteam's structured output for subsequent ones in mixed). Sub-agent factories close over the relevant Slice (Q14 lambda-default-arg pattern, see S1 fix below).
    4. **`rollup`** — LLM-driven Agent (haiku) with tool `[mem_save_mcp_tool]` and `output_type=RollupResult`:
       ```
       RollupResult {
         summary: SessionSummary,                       # required — always written
         new_patterns: list[TaskPattern] (max_items=3), # optional — only reusable URLs/selectors/format recipes
         new_errors:   list[ErrorRecord] (max_items=3), # optional — only failures worth recalling
         new_rules:    list[UserRule]    (max_items=2), # optional — only explicit durable preferences from HITL edits
       }
       ```
       After emission, code iterates and writes each: 1× `mem_save(kind=session_summary, ...)` + N× `mem_save(kind=task_pattern, ...)` + M× `mem_save(kind=error_resolution, ...)` + K× `mem_save(kind=user_rule, ...)`. Instructions explicitly bound when each list should be non-empty (e.g., `new_rules` only if a HITL response contained an explicit user directive). All writes share `sess_id` for traceability. Session_summary body includes the cost line.
  - Both classifier functions run on haiku — combined worst-case cost on `mixed` ~$0.0002 per Execution.
  - Test (Phase 5): unit-test `classify_prompt` and `plan_mixed_steps` with mocked anthropic SDK; integration-test `memory_loader` with `mock_run(memory_loader, prompt, events=[MockEvent.tool_call("mem_context", ...), MockEvent.done(MemoryLoaderResult(...))])`.
- `src/droids_agents/slicing.py`:
  - Bundle shape (from droids-mem `ContextResponse`, see `internal/store/context.go:28-43`): `{task_type, last_session?, user_rules[], browse[]}`. **Tier matters**:
    - `last_session` is `Tier="always"` → carries `.learned` (full body), `.snippet` is empty.
    - `user_rules[]` items are `Tier="always"` → carry `.learned`, `.snippet` is empty.
    - `browse[]` items are `Tier="browse"` → carry `.snippet` (≤120 chars truncated from `what`), `.learned` is empty (omitempty).
    - Slicing code MUST read the correct field per tier: `text = m.learned if m.tier == "always" else m.snippet`. Reading `.learned` on a browse item yields an empty string and silently injects nothing.
  - `browse[]` mixes `kind=error_resolution` and `kind=task_pattern` rows — slicing filters by `kind` locally.
  - `ContextMemory` does NOT expose tags; user_rules are returned wholesale per task_type (no tag filtering in V1).
  - `slice_for(role: Role, bundle: ContextResponse, prompt: str) -> SlicedContext`.
  - Static rules per role (V1) — text extracted via the tier-aware reader:
    - `competitor` (Researcher) → `last_session` + `[b for b in browse if b.kind == "task_pattern"]` filtered by prompt token substring match on `b.title + b.snippet`.
    - `extractor` (Doc-Extractor) → `[b for b in browse if b.kind == "error_resolution"]` + `[b for b in browse if b.kind == "task_pattern"]`.
    - `synthesizer` (Doc-Synth) → `last_session` + `[b for b in browse if b.kind == "error_resolution"]`.
    - `form_planner` (Form-Planner) → `user_rules` + `[b for b in browse if b.kind == "task_pattern"]`.
    - `form_executor` (Form-Executor) → `user_rules` + `[b for b in browse if b.kind == "task_pattern"]` (field-format rules).
    - `drafter` (Email-Drafter) → `user_rules` + `last_session`.
    - `sender` (Email-Sender) → `user_rules` (recipient allowlist rules, tone) + `last_session`.
  - `router_classifier` is NOT in this map — classifier is a plain Python function in CLI, not an agentspan Agent (see router.py above).
  - Returns compact list of strings the parent injects into the Sub-agent's `instructions` closure (per Q14 factory pattern + S1 default-arg fix).

### Phase 4 — CLI + lifecycle + cost cap

- `src/droids_agents/cli.py` (click) — main group with subcommands:
  - **`droids-agents run <prompt>`** (default; bare `droids-agents <prompt>` aliases to `run`) — the Execution entry point.
  - **`droids-agents auth gmail`** — one-time interactive OAuth desktop flow. Loads `GOOGLE_CREDENTIALS_JSON`, opens browser, captures consent, writes token to `GOOGLE_TOKEN_JSON` path. Idempotent: re-running rotates the token. Agents themselves NEVER invoke this flow.
  - **`droids-agents doctor`** — pre-flight checks; exit 0 / 1 with structured JSON list of pass/fail entries:
    - `ANTHROPIC_API_KEY` present.
    - `droids-mem-mcp` reachable at `DROIDS_MEM_MCP_URL` + `/healthz` returns 200 (mirrors droids-mem doctor pattern).
    - `agentspan` server reachable at `AGENTSPAN_URL` (HTTP GET on root or known healthz).
    - `GOOGLE_TOKEN_JSON` exists, loadable, refreshable (does not call Gmail — only validates token shape + refresh).
    - `playwright install chromium` has been run (check for chromium binary in playwright cache).
  - Args per PRD CLI contract section.
  - `--docs` parser validates eagerly (before any LLM call): each path exists, is a file (not dir), extension ∈ `{.pdf, .md, .txt}`, basenames are unique across the list, total raw size ≤ 5 MB. Fails with usage error (exit 2) on any violation. On pass, the validated `[(path, basename), ...]` list is plumbed into the doc_team's `extractor` factory via its `instructions` closure (paths + basenames listed). Runtime enforces the per-doc 50k char cap in `read_doc`.
  - `--dry-run` plumbing: a `dry_run: bool` flag travels in the runtime context. The pipeline runs end-to-end — classifier, `mem_context`, slicing, full Subteam (drafter, form_planner, extractor, synthesizer, etc.) — so the user sees a real preview (email body, form fields, findings). Short-circuits when `dry_run=True`:
    1. HITL-gated tools (`gmail_send`, `web_submit`) return `{"status": "dry_run_skip", "proposed_args": ...}` without performing the side effect.
    2. The `rollup` step's `mem_save` calls all no-op (no droids-mem writes). Other Sub-agents never call `mem_save` (broker pattern) so they need no change.
    3. CLI prints the structured `RollupResult` (or per-Subteam structured output for non-mixed runs) as JSON to stdout and exits `10`.
  - **Read-only tools still execute under `--dry-run`** (S3): `web_navigate`, `web_extract_text`, `gmail_list`, `read_doc`, `mem_context` (read), `mem_search` (read). Real HTTP requests / file reads happen — that's how the preview content is generated. If a user wants a no-network preview, they need to mock the network or skip the dry-run feature.
  - Cost is still incurred during dry-run (LLM calls happen). `--max-cost-usd` still applies.
  - Flow: load settings → build root agent with mem tools (Root agent itself calls `mem_context` as its first action — see Memory broker pattern) → compute `max_total_tokens` from `--max-cost-usd` if set via `usd_to_max_total_tokens()` → set `termination=MaxMessageTermination(max_turns) | TokenUsageTermination(max_total_tokens=N)` on Root (`Agent(..., termination=...)`); tree-wide aggregation is documented (skills.md) → `runtime.start(root, prompt, context={"task_type_override": ..., "session_id_override": ...})` → print a one-line header to stdout immediately when `runtime.start()` returns (`Execution exec_01HG… started (task_type_override=… if provided)`) so the user has exec_id from t=0; print a second line once Root's memory_loader step emits sess_id (`sess_id=sess_01HG…, task_type=…`) — both go to stderr in human mode, stdout JSON events in `--json` mode → stream events to stdout (JSON if `--json`); on pause events (`is_waiting`), emit a delimited human-readable block with Droid name, role, tool name, args (body/preview truncated to 200 chars), sess_id, exec_id, and the direct `http://localhost:6767/executions/<exec_id>` URL so the user can disambiguate concurrent HITL pauses from the terminal → buffer log lines until Root emits `sess_id` then flush to `<sess_id>.jsonl` → Rollup (`mem_save(kind=session_summary, sess_id=...)`) is performed by the Root agent itself on completion; Rollup body includes an aggregate cost line (`Cost: $X / Y prompt + Z completion tok / M msgs`). Full per-call cost ledger lives in `<sess_id>.jsonl` only.
  - `--task-type` short-circuits classifier (Root skips classification, uses provided label for `mem_context`).
  - `--session-id sess_X` provided → CLI skips sess_id minting; Root threads `sess_X` into `mem_context` (Bundle is still loaded scoped to task_type) and into ALL `mem_save` calls. Multiple Executions sharing one `--session-id` accumulate memories under that one sess_id (logical grouping across days/runs). No pre-existence check — droids-mem is stateless on sess_id. This is **group-by semantics**, NOT agentspan pause-resume; for paused HITL pickup, use the `:6767` UI directly.
  - Connects to existing agentspan server at `AGENTSPAN_URL`; refuses to start if server unreachable (message: "agentspan server not running — start with `agentspan server start`").
- `src/droids_agents/runtime.py`:
  - Thin `connect_runtime(settings) -> AgentRuntime` wrapping `AgentRuntime(server_url=settings.agentspan_url)`.
- `README.md` documenting boot sequence (agentspan server is pinned to `~/.droids-agents/` so `agent-runtime.db` lives at a known stable path regardless of CLI cwd):
  ```
  # 1. Start droids-mem MCP bridge
  cd droids-mem && go build ./cmd/droids-mem-mcp
  DROIDS_MEM_MCP_TOKEN=$(openssl rand -hex 16) ./droids-mem-mcp &
  # 2. Start agentspan server pinned to ~/.droids-agents (dedicated terminal)
  mkdir -p ~/.droids-agents
  cd ~/.droids-agents && agentspan server start
  # → agent-runtime.db lands at ~/.droids-agents/agent-runtime.db
  # → co-located with structlog log dir ~/.droids-agents/logs/
  # 3. Run a task (from any cwd — CLI connects via AGENTSPAN_URL)
  uv run droids-agents "research Anthropic vs OpenAI pricing"
  ```

### Phase 5 — tests

- `tests/unit/test_router.py`: `mock_run(root, prompt, events=[MockEvent.handoff("research_team"), ...])` for each label.
- `tests/unit/test_slicing.py`: pure-function tests of `slice_for(role, fixture_bundle)`.
- `tests/unit/test_research_parallel.py`: `mock_run` with parallel sub-agent fan-out, assert each `competitor_*` called with sliced ctx.
- `tests/unit/test_hitl.py`: `mock_run` with `MockEvent.tool_call("gmail_send", ...)` → assert pause (`expect(result).is_waiting()`).
- `tests/unit/test_guardrails.py`: pure-function tests of each guardrail (`no_jailbreak`, `findings_complete`, `pii_in_draft`, `recipient_allowlist`, `tone_length`, `citations_present`, `pii_in_form_fields`). Drive `mock_run` to confirm `OnFail.HUMAN` triggers `expect(result).is_waiting()` and `OnFail.RETRY` triggers re-attempt.
- `tests/unit/test_naming.py`: `NamePool` claim/release uniqueness, exhaustion suffix behavior, yaml loading from fixture, `agent_display()` formatting (`"C-3PO: [Researcher]"`).
- `tests/integration/conftest.py`:
  - **Session-scoped** `agentspan_server` fixture: subprocess `agentspan server start --port <ephemeral>` once per pytest session (JAR cached after first run). Workers process tests serially.
  - **Function-scoped** `mem_server` fixture: spawns droids-mem-mcp per test on its own ephemeral port pointed at a tempfile `DROIDS_MEM_DB` — port-pick + healthz poll mirroring `droid-infra/droids-mem/cmd/droids-mem-mcp/e2e_test.go`. Each test gets a clean droids-mem DB → can assert "rows == N for sess_id X" precisely.
  - **Per-test teardown (M7)**: function-scoped `autouse` fixture wraps each test with a `try ... finally` that, after the test, lists agentspan executions started during this test (filtered by a `test_marker` in execution metadata) and calls `runtime.cancel(exec_id, reason="test teardown")` on any still in WAITING or RUNNING state. Prevents prior-test paused HITL Executions from interfering with subsequent tests.
  - Test isolation rule: agentspan's internal SQLite is shared session-wide; tests assert against their own `exec_id` / `sess_id` only, never on aggregate counts.
- `tests/integration/test_e2e.py` (`@pytest.mark.integration`): real `droids-agents <prompt>` against the session-scoped agentspan + per-test droids-mem; mock the model via agentspan's mock model adapter (zero cost, deterministic). No `ANTHROPIC_BASE_URL` proxy in V1.

## Critical files to reuse / reference

- `droid-infra/droids-mem/cmd/droids-mem-mcp/main.go` — auth header format, endpoint `/mcp`, 4 tool names + schemas (already matched by `mcp_tool` auto-discovery).
- `droid-infra/droids-mem/cmd/droids-mem-mcp/e2e_test.go` — port-pick + healthz-poll pattern for ephemeral server in integration fixtures.
- `droid-infra/droids-mem/internal/store/save.go:314` — kind enum (validation only in Go, no need to mirror in Python; rely on MCP error response).
- `droid-infra/droids-mem/CONTEXT.md` — domain glossary; reuse terms (`Session`, `Memory`, `kind`) verbatim in droids-agents code/docs.
- `droid-infra/droids-mem/docs/adr/0003-mcp-bridge-for-agentspan.md` — bearer auth + stateless server rationale; informs Python client expectations.

## Verification

End-to-end check sequence after Phase 5:

1. **Boot**: `droids-mem-mcp` on :7777, `agentspan server start` on :6767, `.env` populated, `playwright install chromium` once.
2. **Router smoke** (mocked): `pytest tests/unit/test_router.py -k research` passes — `mock_run` shows classifier handoff to `research_team`.
3. **Slice rules**: `pytest tests/unit/test_slicing.py` — fixture bundle → correct slice per role.
4. **Parallel fan-out** (mocked): `pytest tests/unit/test_research_parallel.py` — assert each competitor_agent invoked with its slice, results aggregated.
5. **HITL pause** (mocked): `pytest tests/unit/test_hitl.py` — `expect(result).is_waiting()` on `gmail_send`.
6. **Live integration** (`@pytest.mark.integration`):
   - `pytest tests/integration -m integration`.
   - Starts ephemeral droids-mem-mcp + agentspan, runs full CLI for prompt "research Anthropic pricing", asserts: classifier picks `research`, parent mints sess_id, sub-agents complete without touching droids-mem, parent writes one `session_summary` row with correct `sess_id`, exit 0.
7. **Restart recovery**: kill agentspan worker mid-execution → restart → HITL pause persists; approve via :6767 UI → execution resumes; sub-agent results still available from durable log; rollup fires.
8. **Cost cap**: `droids-agents "..." --max-cost-usd 0.01` → `TokenUsageTermination` fires, exit 5, log file shows ledger.
9. **Concurrent CLIs**: two `droids-agents` shells in parallel → distinct sess_id per run, droids-mem rows correctly attributed.

## Out of scope (V2)

(As per PRD `Open items` section.) Don't drift into: Slack/SMS, URL ingestion, data analysis subteam, custom approval UI, multi-machine deploy, OTel/Langfuse.
