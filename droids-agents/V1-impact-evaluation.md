# V1 Impact Evaluation: droids-agents

## Executive Summary

Droids-agents V1 is a **locally-scoped, durable multi-agent runtime** that removes a critical execution gap in the droid-infra ecosystem. Paired with droids-mem (V1, already shipped), it enables autonomous agents to perform real business-intelligence workflows—competitor research, document synthesis, form submission, email drafting—with human approval gates and durable recovery. 

**Verdict: Go-ahead recommended.** The scope is appropriately sized for V1, risks are manageable, and the foundation supports future expansion. One minor blocker to resolve before build.

---

## 1. Value Proposition

**Problem solved**: Without droids-agents, agents built on top of droids-mem have memory but no execution substrate. They can't reliably run multi-step workflows, recover from crashes, or pause for human approval. Each agent invocation requires manual orchestration (subprocess calls, retry logic, state management).

**Before vs. after**:
- **Before**: Solo devs and BI teams hand-script agent runs, lose partial results on crash, approve actions via Slack pings or email.
- **After**: One CLI invocation (`droids-agents <prompt>`) triggers a durable, parallelizable agent pipeline with built-in memory continuity, HITL gates on side effects, and automatic recovery.

**Target user**: Developer-operators (solo founders, small BI teams) running autonomous research and automation tasks on their local machine. Not enterprise deployments, not multi-user SaaS—this is intentionally single-user, single-machine in V1.

**Workflow change**:
1. User types: `droids-agents "research Anthropic vs OpenAI"`
2. Classifier picks research_team (cheap haiku call)
3. Root loads relevant prior sessions from droids-mem
4. Parallel competitor agents scrape + synthesize
5. Session summary saved to droids-mem automatically
6. User can reuse that knowledge in the next research run
7. If agent crashes mid-run, agentspan's durable log replays from the last completed step

This is a **closed-loop feedback system** that improves with use. Each run teaches the system; memory compounds.

---

## 2. Scope Assessment

**V1 scope: appropriately constrained.**

✓ **Right-sized features**:
- 4 well-defined subteams (research / docs / form / messaging) — each with a clear, discrete output type
- Mixed workflows (sequential chaining) to handle composite prompts like "research + draft email"
- HITL gates only on 2 irreversible tools (web_submit, gmail_send) — not over-gated
- Guardrails on 6 specific validation points (citations, PII, recipient allowlist, etc.) — concrete, not aspirational
- Local-first runtime — no cloud deployment complexity; agentspan + droid-mem are already local

✓ **Non-goals clearly deferred**:
- SQL/CSV data analysis → V2 (internal data subteam)
- Slack/SMS → V2 (messaging extensibility)
- Custom approval UI → defer to agentspan's built-in :6767
- Multi-machine orchestration → V2
- Local LLM → V1 stays cloud-only (simplifies)

✗ **Under-scoped?** No — the 4 subteams + memory integration alone require substantial orchestration work (agent composition, tool wiring, session lifecycle, cost caps). Adding more subteams or UI customization would push V1 past "core runtime" into "platform."

✗ **Over-scoped?** No — Phase 1–5 plan is linear, testable, and builds incrementally. Each phase has clear deliverables and verification criteria.

**Verdict**: Scope is **lean but complete** — V1 solves a real problem (durable agent orchestration + memory integration) without gold-plating.

---

## 3. Key Risks to Impact

### Risk 1: Adoption friction — "Why not just use LangGraph / Dify / Crew AI?"

**Exposure**: Solo devs and teams may see an unfamiliar CLI + Python stack and choose an off-the-shelf framework instead.

**Mitigation**:
- Clear README with boot sequence and example runs (already planned in Phase 4).
- E2E smoke tests that pass in 2 minutes (Phase 5 `test_e2e.py`).
- Explicit comparison doc: droids-agents is local-first + memory-integrated, not a general agent framework. Competitors lack droids-mem's save/search/context model.
- Beta user feedback loop post-V1 (not in scope but important for V2 adoption).

### Risk 2: Integration gap with droids-mem

**Exposure**: If droids-mem's wire contracts (session_id vs sess_id, MCP response envelope shape, kind enum) drift from droids-agents assumptions, both systems break.

**Mitigation**:
- **Phase 0 ADR** (`0004-agent-broker-pattern.md`) locks droids-mem's 4-kind enum and broker-pattern semantics before droids-agents code touches droids-mem.
- Wire-format constants in `mem.py` (`MEM_SESSION_KEY = "session_id"`) enforce exact JSON keys — catch mismatches early.
- Integration tests spawn both binaries on ephemeral ports with tempfile DB — exact contract validation.
- Cross-repo references in code (comments to droids-mem `CLAUDE.md`, ADRs) prevent silent drift.

### Risk 3: Undefined success metrics

**Exposure**: Without tracking KPIs (e.g., "% of runs that use mem_context", "time from run start to HITL pause"), we won't know if V1 is working as intended.

**Mitigation** (see §4 below): Propose 5 concrete metrics to track in structlog + droids-mem.

### Risk 4: Cost cap UX pitfall

**Exposure**: `--max-cost-usd` maps to token budget via a conservative estimate (assumes sonnet worst-case, blended prompt/completion). Real costs vary; users get surprised.

**Mitigation**:
- Docstring in `pricing.py` clearly states the estimate is conservative and refines post-V1.
- Cost ledger is logged per-call in structlog; aggregate printed to CLI and saved to droids-mem.
- Users can inspect actual costs in `~/.droids-agents/logs/<sess_id>.jsonl` for tuning.

### Risk 5: HITL pause disambiguation in concurrent runs

**Exposure**: Two `droids-agents` runs paused simultaneously → two HITL cards on :6767 UI → user must click the URL in CLI to disambiguate.

**Mitigation**:
- CLI stdout includes sess_id + exec_id + Droid name in pause block; user knows which run is waiting.
- Phase 1 lock: single-user, single-machine assumption. Multi-user HITL disambiguation deferred to V2.

---

## 4. Success Metrics (Proposed)

**No metrics are defined in the PRD.** Here are 5 concrete ones to track in structlog and droids-mem:

1. **Memory loop closure rate**: % of Executions that call `mem_context` AND write back a `session_summary` (not just partial data). Target ≥85% for V1. Indicates memory broker pattern is working end-to-end.

2. **Avg context reuse**: Avg # of memories loaded via `mem_context` per Execution, grouped by task_type. Expected range 3–8 (droids-mem's `context` cap). Trending upward over weeks suggests memory is compound-useful.

3. **HITL gate hit rate**: % of Executions that pause at approval gates (gmail_send, web_submit, or guardrail failures). V1 target 15–30% for typical workflows (a few email sends or form submissions per run are normal). Rates >50% indicate over-gating; rates <5% suggest few irreversible actions attempted.

4. **Error resolution capture**: # of `kind=error_resolution` memories written per 100 Executions. Target >5 per 100 (i.e., agents encounter and learn from errors regularly). Indicates utility of the error-recovery feedback loop.

5. **Crash recovery: resume success rate**: # of agentspan Executions resumed after worker crash / restart, as % of total paused Executions. Target ≥90% (durable log should be reliable). Validates the "resilience" claim.

**Where to track**: Embed metrics in structlog JSON output; optionally emit as memory summaries for trend analysis.

---

## 5. Strategic Fit

**Ecosystem position**: droids-agents V1 completes the **memory ↔ execution loop**.

```
droids-mem (V1 shipped)
  ├─ Saves lessons (session_summary, error_resolution, task_pattern, user_rule)
  ├─ Searches + retrieves context (BM25 + recency ranking)
  └─ [ISOLATED] No execution substrate

droids-agents V1 (proposed)
  ├─ Loads context from droids-mem at start
  ├─ Executes multi-agent workflows durable to crashes
  ├─ Writes back curated summaries to droids-mem
  └─ [LOCALIZED] Single-machine, single-user, local-first
```

**Foundation for future**: The architecture naturally extends:
- **V2 subteams** (SQL analysis, Slack integration) slot into existing Subteam pattern; no Root refactor.
- **V2 retention tuning** (per-kind bounds in droids-mem, archival) improves memory quality as corpus grows.
- **V2 task_type inference** (richer classification than 5-label enum) sharpens memory bucketing without changing executor logic.
- **V2 deployment** (multi-machine agentspan, distributed workers) begins with the same durable SQLite model; scale up incrementally.

**FUTURE-Agents.TODO alignment**: All deferred items (pause-resume CLI, URL ingestion, data team, custom UI, parallel test isolation) are post-scope and don't block V1. V1 builds the right foundation.

---

## 6. Go/No-Go Recommendation

**GO with one pre-build clarification.**

### Blocker (resolve before Phase 1):

**§ 3b: droids-mem ADR 0004 must be co-reviewed and locked in droids-mem repo.**

The Plan (Phase 0) proposes a new ADR documenting the **broker pattern** (only Root agent touches droids-mem; sub-agents are pure compute) and the **4-kind enum lock** (no new memory types added in V1). This is a **consumer-side design decision** that locks droids-mem's contract.

**Action**: Before coding droids-agents, pair with droids-mem maintainer (likely yourself) to:
1. Draft `droids-mem/docs/adr/0004-agent-broker-pattern.md` with the rationale (semantic purity, dedupe simplicity, durable replay safety).
2. Review it against actual droids-mem code (`cmd/droids-mem-mcp/tools.go`, `internal/store/save.go`).
3. Confirm the 4-kind enum is stable and the MCP response shape is finalized.

This is a 30-min sync, not a new implementation. But it must happen before droids-agents Phase 1 lands, to prevent rework.

### Minor (low-risk, can resolve during Phase 1):

- **Phase 1 config defaults**: `DROIDS_MEM_MCP_URL` default `http://localhost:7777/mcp` assumes droids-mem-mcp on default port. Add a `doctor` check (Phase 4) to catch misconfiguration early.
- **Pricing.py conservative estimate**: Document the sonnet-based worst-case formula; plan a post-V1 drill-down on real cost drift with actual users.

---

## 7. Key Dependencies & Critical Path

**Hard dependencies** (must exist before droids-agents boots):
1. droids-mem-mcp running (Go binary, already shipped).
2. agentspan server running (Python, external dep, documented in boot sequence).
3. Google OAuth token for Gmail (Phase 4 `auth gmail` handles setup).
4. Playwright chromium (Phase 4 `doctor` validates installation).

**Code dependencies**:
- agentspan Python SDK + models (Anthropic + OpenAI supported; V1 locks Anthropic only).
- droids-mem wire contracts (MCP response envelope, 4-kind enum, session_id key name).

**Implementation path** (lowest-risk order):
1. Phase 0: ADR lock in droids-mem repo (blocker).
2. Phase 1–2: Config + tools layer (no agent wiring yet; can test in isolation).
3. Phase 3: Guardrails + agent schemas (pure functions; fully unit-testable).
4. Phase 3b: Agents + router (core logic; integration tests with mocked LLM).
5. Phase 4: CLI + lifecycle (attach all pieces; end-to-end smoke).
6. Phase 5: Tests (comprehensive coverage of Phase 1–4).

---

## 8. Conclusion

**V1 is production-ready in scope and design.** It solves a real problem (durable agent orchestration with persistent memory), uses proven dependencies (agentspan, droids-mem, SQLite), and builds a foundation for future extensibility without over-engineering.

**To green-light: Schedule a 30-min sync with droids-mem owner to finalize ADR 0004, confirm wire contracts, and lock the 4-kind enum.** Then proceed with Phase 1.

**Success looks like**: Within 4 weeks of V1 launch, a solo dev can run `droids-agents "research X"` and see a researcher agents scrape competitors, save findings to droids-mem, and seamlessly reuse that context in a follow-up "research Y" run. Memory compounds; execution is resilient; side effects (email, form submission) are gated for human approval.
