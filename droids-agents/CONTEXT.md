# droids-agents

Local-first multi-agent runtime that drives business-intelligence workflows (research, doc synthesis, form submission, email) on top of `agentspan` for durability and `droids-mem` for cross-run memory.

## Language

### Execution surface

**Execution**:
One top-level run of `droids-agents <prompt>` from CLI start to final result.
_Avoid_: session, job, request, invocation

**Root agent**:
The top-level agent constructed per Execution that owns routing and the Memory broker role.
_Avoid_: orchestrator, supervisor, manager

**Subteam**:
A composite agent grouping sub-agents under one strategy; exactly one of `research_team`, `doc_team`, `form_team`, `messaging_team`.
_Avoid_: team, squad, group

**Sub-agent**:
A leaf agent inside a Subteam that does the actual work (scrape a competitor, draft an email body).
_Avoid_: worker, child agent, actor

**Strategy**:
agentspan's wiring mode between a parent and its agents; one of `parallel`, `sequential`, `handoff`, `router`, `swarm`.
_Avoid_: pattern, mode, topology

**Mixed prompt**:
A user prompt the Router classifier labels `mixed`, triggering a Sequential pipeline across Subteams instead of one Subteam.
_Avoid_: multi-team, compound, chained

### Routing

**Router classifier**:
The cheap haiku agent at the top of the Root agent that emits exactly one of `research | docs | form | messaging | mixed`.
_Avoid_: dispatcher, selector, triage

**Handoff**:
Transfer of control from a parent agent to one sub-agent it chose itself; distinct from Router (separate classifier picks).
_Avoid_: routing (when classifier-based), delegation

### Memory + slicing

**Bundle**:
The full `ContextResponse` returned by `mem_context` (last session_summary, user_rules, browse list).
_Avoid_: context, memory dump

**Slice**:
A role-specific subset of a Bundle, produced by `slice_for(role, bundle)` and injected into one Sub-agent's system prompt.
_Avoid_: filter, view, projection

**Memory broker**:
The Root agent acting as sole reader/writer of `droids-mem` for an Execution; Sub-agents never call mem tools directly.
_Avoid_: memory owner, mem gateway

**Rollup**:
The Root agent's final `mem_save(kind=session_summary, sess_id=…)` call that condenses Sub-agent returns into one curated record.
_Avoid_: summary write, finalize

### HITL + guardrails

**HITL gate**:
Any mechanism that pauses an Execution for human approval/edit on the agentspan UI; either an Approval-required tool or a Guardrail with `OnFail.HUMAN`.
_Avoid_: human checkpoint, approval step

**Approval-required tool**:
A tool decorated `@tool(approval_required=True)` (e.g. `gmail_send`, `web_submit`) that always pauses before invocation.
_Avoid_: guarded tool, sensitive tool

**Guardrail**:
A validation function attached to an agent at `Position.INPUT` or `Position.OUTPUT` returning `GuardrailResult(passed, message)`.
_Avoid_: check, filter, validator

**OnFail mode**:
The action taken when a Guardrail fails; one of `RAISE`, `RETRY`, `FIX`, `HUMAN`.
_Avoid_: fail action, on-fail

### Identity

**NamePool**:
The per-Execution pool of Star Wars droid names sourced from `droids-name.yml`; assigns one unique Droid name per Sub-agent.
_Avoid_: name registry, name set

**Droid name**:
The Star Wars name (e.g. `C-3PO`) claimed from the NamePool and surfaced via the agent's `metadata={"droid_name": ...}` field + CLI/structlog output. The agentspan `Agent.name` field itself is a stable identifier (e.g. `competitor_0`) because agentspan uses `name` as the Conductor workflow cache key — Droid names are randomized per-Execution and would defeat the compile cache.
_Avoid_: alias, handle, display name

**Role**:
The functional label for an agent (`Researcher`, `Doc-Extractor`, `Form-Planner`, `Email-Drafter`, etc.) shown next to the Droid name as `C-3PO: [Researcher]`.
_Avoid_: type, category, kind

### Cost

**Token cap**:
A `TokenUsageTermination(max_total_tokens=…)` derived from `--max-cost-usd` via the Anthropic price table; bounds an Execution's LLM cost.
_Avoid_: budget, limit, quota

## Relationships

- An **Execution** has exactly one **Root agent** and exactly one droids-mem `session_id` (the Run's `Session`).
- A **Root agent** invokes exactly one **Subteam** per Execution — unless the prompt is **Mixed**, in which case it runs a Sequential pipeline across multiple Subteams.
- A **Subteam** contains one or more **Sub-agents**, wired by a **Strategy**.
- Every **Sub-agent** holds exactly one **Droid name** from the **NamePool** plus exactly one **Role**.
- The **Memory broker** (Root agent) is the only agent that reads or writes droids-mem; it produces one **Slice** per Sub-agent from the **Bundle**, and writes one **Rollup** at the end.
- A **HITL gate** is either an **Approval-required tool** OR a **Guardrail** with `OnFail.HUMAN`; both pause via agentspan's WaitTask on the same UI.

## Example dialogue

> **Dev:** "If the Router classifier picks `messaging`, can the **Sub-agent** look up the customer's last interaction from droids-mem before drafting?"
> **Domain expert:** "No — only the **Memory broker** touches droids-mem. The broker reads the **Bundle** up front and gives the drafter a tailored **Slice** in its system prompt. The drafter is pure compute."

> **Dev:** "What's the difference between an **Approval-required tool** and a **Guardrail** with `OnFail.HUMAN`? They both pause."
> **Domain expert:** "The tool pauses every time it's called — deterministic. The Guardrail only pauses when a rule fails — conditional. We stack both: `gmail_send` always pauses (tool decorator) AND the `pii_in_draft` Guardrail can pause earlier if the body has PII."

## Flagged ambiguities

- "agent" was overloaded between the orchestrator, the subteam containers, and the leaf workers — resolved: **Root agent**, **Subteam**, **Sub-agent** are distinct.
- "session" conflicted between agentspan's execution and droids-mem's `session_id` — resolved: **Execution** refers to the agentspan runtime concept; **Session** (capitalized, droids-mem term) refers to the memory-side grouping. One Execution produces one Session.
- "handoff" vs "router" — resolved: **Handoff** means the parent's own LLM picks the sub-agent; **Router** means a dedicated cheap classifier picks. Different costs, different determinism.
- "team" was used both for Subteam containers and informally for the whole agent tree — resolved: **Subteam** is reserved for the four V1 containers (`research_team`, etc.); the whole tree is the Root agent.
