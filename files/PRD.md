# droids-infra — Product Requirements Document

> **Version:** 1.0 · **Status:** Draft · **Last Updated:** May 2026

---

## Table of Contents

1. [Product Overview](#1-product-overview)
2. [Problem Statement](#2-problem-statement)
3. [Goals & Success Metrics](#3-goals--success-metrics)
4. [Architecture Overview](#4-architecture-overview)
5. [Feature Flag & Tool System](#5-feature-flag--tool-system)
6. [Agentspan Integration](#6-agentspan-integration)
7. [Task Domains](#7-task-domains)
8. [Capability Tiers](#8-capability-tiers)
9. [Integration Layer](#9-integration-layer)
10. [User Experience Requirements](#10-user-experience-requirements)
11. [Development Milestones](#11-development-milestones)
12. [Risks & Mitigations](#12-risks--mitigations)
13. [Open Questions](#13-open-questions)
14. [Glossary](#14-glossary)

---

## 1. Product Overview

**droids-infra** is a context-aware agent orchestration platform that enables small and medium-sized businesses (SMBs) to deploy AI agents that automate high-value business workflows — without writing code.

The platform is built on two foundational layers:

- **Agentspan** — the execution engine handling durable workflows, crash recovery, human-in-the-loop pausing, observability, and multi-agent coordination.
- **droids-infra's Feature Flag & Tool System** — the intelligence layer that determines which tools each agent can access, controlled entirely by feature flags tied to the deployment context (user type, plan tier, task domain).

Agents operate at **medium-to-high scale**, targeting SMBs that need reliable, repeatable automation across four core domains: CRM uploads, customer information management, email automation, and data processing.

---

## 2. Problem Statement

SMBs spend significant time on repetitive, data-heavy tasks:

- Uploading contacts and deals to CRMs from spreadsheets or form exports
- Processing and cleaning incoming customer data from multiple sources
- Sending trigger-based follow-up emails and onboarding sequences
- Normalizing and routing messy data exports

Existing solutions fall into two failure modes:

| Solution Type | Failure Mode |
|---|---|
| Developer tools (LangChain, n8n, custom scripts) | Require technical expertise most SMBs don't have |
| Rigid automation tools (Zapier templates) | Too inflexible for the variety of real SMB workflows |
| Enterprise AI platforms | Overbuilt, overpriced, not designed for SMB scale |

No platform currently provides the combination of **flexibility, safety, and simplicity** that lets an SMB owner trust an AI agent with their real customer data.

---

## 3. Goals & Success Metrics ( TO EDIT )

### 3.1 Business Goals

- Reach **50 paying SMB customers** within 6 months of beta launch
- Achieve **$15K MRR** by end of Q1 post-launch
- Establish Growth tier as primary revenue driver (target: 60% of customer base)
- Maintain agent task error rate **below 2%** across all automated completions

### 3.2 Product Goals

- Non-technical user deploys their first agent in **under 10 minutes**
- Zero unauthorized tool calls reach production integrations
- Users understand what their agent did without needing support — purely from the activity log
- Feature flag system is extensible: adding a new tool requires no architectural changes

### 3.3 Key Metrics

| Metric | Target |
|---|---|
| Time to first agent deployed | < 10 minutes from account creation |
| Agent task success rate | ≥ 98% |
| Human review approval rate | ≥ 85% (proposals accepted without modification) |
| Monthly churn | < 5% |
| NPS at 30 days | ≥ 40 |
| Support ticket rate | < 1 per 100 agent runs |

---

## 4. Architecture Overview

droids-infra is composed of two distinct product layers sitting on top of Agentspan's execution engine.

```
┌─────────────────────────────────────────────────────────┐
│                      droids                          │
│                                                         │
│  ┌─────────────────┐     ┌──────────────────────────┐  │
│  │  No-Code UI     │     │  Feature Flag &          │  │
│  │  - Setup wizard │     │  Tool System             │  │
│  │  - Review queue │     │  - Context resolver      │  │
│  │  - Dashboard    │     │  - Tool surface builder  │  │
│  └─────────────────┘     │  - Runtime enforcement   │  │
│                           └──────────────────────────┘  │
│                                      │                   │
│            ┌─────────────────────────┘                   │
│            ↓                                             │
│  ┌─────────────────────────────────────────────────┐    │
│  │            Integration Adapters                  │    │
│  │    HubSpot · Gmail · Google Sheets · ...        │    │
│  └─────────────────────────────────────────────────┘    │
└──────────────────────────┬──────────────────────────────┘
                           │ tools=[ permitted_tools_only ]
                           ↓
┌─────────────────────────────────────────────────────────┐
│                      AGENTSPAN                          │
│                                                         │
│   Durable execution · HITL pause/resume                 │
│   Observability · Guardrails · Multi-agent              │
│                                                         │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ↓
                     [ Conductor OSS ]
              (Netflix/LinkedIn/Tesla proven)
```

### 4.1 Responsibility Split

| Concern | Owner |
|---|---|
| Durable workflow execution | Agentspan |
| Crash recovery & state persistence | Agentspan |
| Human-in-the-loop pause/resume | Agentspan |
| Step-level observability & logs | Agentspan |
| Guardrails & structured output | Agentspan |
| Multi-agent coordination (Scale tier) | Agentspan |
| Feature flag definitions & control | droids-infra |
| Context resolution (profile → flags) | droids-infra |
| Tool surface assembly | droids-infra |
| Runtime tool call enforcement | droids-infra |
| Integration adapters | droids-infra |
| No-code configuration UI | droids-infra |
| Plan tier & business logic | droids-infra |

### 4.2 Core Architectural Principles

**Feature flags control everything tools-related.** Every tool available in the platform is defined, enabled, and scoped by feature flags. Agents receive only the tools their flags permit. This is enforced at two points: assembly time (tool surface builder) and call time (enforcement layer).

**Agentspan is infrastructure, not product.** droids-infra's product moat is the feature flag system, context resolution logic, integration adapters, and no-code UX. Agentspan handles the mechanics of reliable execution underneath.

**Supervised by default.** All write operations (CRM, email send) route to the human review queue unless the deployment's flags explicitly permit autonomous execution.

**Hard enforcement, not instructional.** The LLM is never "told" not to use a tool. Disallowed tools are simply not present in the agent's context. A second enforcement check at call time catches anything that bypasses the surface builder.

---

## 5. Feature Flag & Tool System

This is droids-infra's core IP. The feature flag system is a fully self-contained subsystem that controls which tools every agent can access, defined by flags and resolved at deployment time.

### 5.1 How It Works

Every tool in droids-infra is registered in a **tool registry** — a catalog of available tools with their definitions, risk profiles, and flag requirements. At deployment time, the system evaluates the active flags for a given deployment context and assembles the permitted tool list. That list is what gets handed to Agentspan.

```
Tool Registry
  └─ tool: crm_write
       flag_required: "crm.write.enabled"
       risk: HIGH
       review_required: true (unless "crm.write.autonomous" flag active)

  └─ tool: email_send
       flag_required: "email.send.enabled"
       risk: MEDIUM
       review_required: true (unless "email.send.autonomous" flag active)

  └─ tool: data_ingest
       flag_required: "data.ingest.enabled"
       risk: LOW
       review_required: false
```

### 5.2 Flag Categories

Flags are grouped by domain and function:

**Data flags**
- `data.ingest.enabled` — CSV/spreadsheet ingestion
- `data.transform.enabled` — Cleaning, normalization, type coercion
- `data.output.enabled` — Export to file or downstream system

**CRM flags**
- `crm.read.enabled` — Read CRM records
- `crm.write.enabled` — Stage CRM writes (always review queue)
- `crm.write.autonomous` — Skip review queue for CRM writes (Scale only)
- `crm.delete.enabled` — Delete CRM records (Scale only, explicit opt-in)

**Email flags**
- `email.draft.enabled` — Generate email drafts
- `email.send.supervised.enabled` — Send after review queue approval
- `email.send.autonomous.enabled` — Send without approval (Scale only)
- `email.parse.enabled` — Parse inbound email for structured data

**Customer data flags**
- `customer.intake.enabled` — Intake new customer records
- `customer.enrich.enabled` — Enrich records with external data (Growth+)
- `customer.pii.enabled` — Handle PII fields (Scale only, compliance gated)
- `customer.merge.enabled` — Merge duplicate records

**System flags**
- `agent.multiagent.enabled` — Spawn sub-agents (Scale only)
- `agent.custom_tools.enabled` — Inject custom tool definitions (Scale only)
- `audit.export.enabled` — Export audit logs (Growth+)

### 5.3 Context Resolution

At deployment time, the context resolver maps three dimensions to a flag set:

- **Plan tier:** Starter / Growth / Scale
- **Business size:** Solo (1–5) / Small (5–50) / Medium (50–250)
- **Task domain:** Data / CRM / Customer Info / Email

The resolved flag set is stored as the **deployment profile** — a flat key-value map of active flags. This is what the tool surface builder reads.

### 5.4 Tool Surface Builder

The tool surface builder reads the deployment profile and produces the `tools=[]` list passed to Agentspan:

```python
def build_tool_surface(deployment_profile: dict) -> list:
    permitted = []
    for tool in TOOL_REGISTRY:
        if deployment_profile.get(tool.flag_required):
            permitted.append(tool)
    return permitted
```

The LLM context only ever sees permitted tools. There is no "hidden" tool to discover or hallucinate into.

### 5.5 Runtime Enforcement

A second enforcement layer wraps every Agentspan tool call before it reaches the integration adapter:

```python
def enforce(tool_name: str, args: dict, profile: dict):
    tool = TOOL_REGISTRY[tool_name]

    # Gate 1: flag check
    if not profile.get(tool.flag_required):
        raise PermissionDenied(f"{tool_name} not permitted for this deployment")

    # Gate 2: review queue routing
    if tool.review_required and not profile.get(f"{tool.flag_required}.autonomous"):
        return route_to_review_queue(tool_name, args)

    # Gate 3: execute
    return execute_integration(tool_name, args)
```

Two gates. Defense in depth. No disallowed tool call reaches a production integration.

---

## 6. Agentspan Integration

droids-infra uses Agentspan as its execution engine. The feature flag system controls what agents can do; Agentspan controls how they do it reliably.

### 6.1 What droids-infra Uses from Agentspan

**Durable execution** — Agent state persists on the Agentspan server. If the worker process crashes mid-run, the agent resumes from the exact step on reconnect. Critical for long-running CRM upload or data processing jobs.

**Human-in-the-loop** — Tools flagged as `review_required` use Agentspan's `@tool(approval_required=True)` decorator. The agent pauses with no timeout, holds state, and resumes when the user approves or rejects from the review queue UI.

**Observability** — Every tool call, LLM request, latency, and token count is logged by Agentspan and surfaced in droids-infra's activity dashboard as plain-English entries.

**Guardrails** — Structured output validation (Pydantic) and regex guardrails catch malformed LLM responses before they reach the integration layer. Auto-retry on failure.

**Multi-agent coordination** — Available to Scale tier deployments. Agentspan's `>>` pipeline operator and `Strategy.PARALLEL` / `Strategy.HANDOFF` patterns are exposed through droids-infra's Scale-tier agent configurations.

**Framework compatibility** — Agentspan's SDK is the only agent framework dependency. No LangChain, LangGraph, or OpenAI Agents SDK required in V1.

### 6.2 Deployment Model

- **V1:** Agentspan self-hosted on droids-infra's infrastructure. Users have no direct Agentspan access.
- **Post-V1:** Evaluate offering self-hosted Agentspan as a Scale-tier option for enterprise customers with data residency requirements.

---

## 7. Task Domains

### 7.1 Data Processing *(lowest risk — V1 entry point)*

Ingest structured data (CSV, XLSX, Google Sheets), apply cleaning and transformation rules, and output normalized datasets or summary reports.

**Flags required:** `data.ingest.enabled`, `data.transform.enabled`, `data.output.enabled`

**Tools:** `file_ingest`, `column_map`, `type_coerce`, `deduplicate`, `output_csv`, `output_summary`

**Risk:** Low. No external writes. Ideal first agent for new users.

---

### 7.2 CRM Uploads *(medium-high risk)*

Pull contact or deal data from a source, normalize and deduplicate it, map fields to the CRM schema, stage proposed records in the review queue, and push on approval.

**Flags required:** `crm.read.enabled`, `crm.write.enabled` (+ `crm.write.autonomous` for Scale)

**Tools:** `crm_read`, `crm_field_map`, `crm_stage`, `crm_write` (gated), `dedup_check`

**Risk:** High. Writes to production customer data. All CRM writes go through review queue in Starter and Growth tiers.

---

### 7.3 Customer Information Management *(medium-high risk)*

Intake new customer records from multiple channels, normalize fields, optionally enrich with external metadata, and maintain consistency across connected systems.

**Flags required:** `customer.intake.enabled`, `customer.enrich.enabled` (Growth+), `customer.pii.enabled` (Scale only)

**Tools:** `record_intake`, `field_normalize`, `enrichment_lookup`, `crm_write` (gated), `duplicate_merge`

**Risk:** Medium-High. PII handling locked to Scale tier.

---

### 7.4 Email Automation *(medium risk)*

Generate trigger-based email drafts or parse inbound emails to extract structured data. Outbound email requires human approval in Starter and Growth tiers.

**Flags required:** `email.draft.enabled`, `email.send.supervised.enabled` (Growth+), `email.send.autonomous.enabled` (Scale only)

**Tools:** `email_draft`, `email_parse`, `email_send_supervised`, `email_send_autonomous` (Scale), `template_render`

**Risk:** Medium. Sending on behalf of the business is consequential. Autonomous sending requires Scale tier and explicit user opt-in.

---

## 8. Capability Tiers

### 8.1 Tier Definitions

| Capability | Starter | Growth | Scale |
|---|:---:|:---:|:---:|
| Data ingestion & cleaning | ✓ | ✓ | ✓ |
| CRM read | ✓ | ✓ | ✓ |
| CRM write (supervised) | ✓ | ✓ | ✓ |
| CRM write (autonomous) | — | ✓ | ✓ |
| Email draft generation | ✓ | ✓ | ✓ |
| Email send (supervised) | — | ✓ | ✓ |
| Email send (autonomous) | — | — | ✓ |
| Customer record enrichment | — | ✓ | ✓ |
| PII handling / compliance tools | — | — | ✓ |
| Multi-agent coordination | — | — | ✓ |
| Custom tool injection | — | — | ✓ |
| Audit log export | Basic | Full | Full + Export |
| Human-in-loop review queue | ✓ | ✓ | ✓ |
| API access | — | — | ✓ |

### 8.2 Tier Flag Profiles (abbreviated)

**Starter**
```
data.ingest.enabled = true
data.transform.enabled = true
crm.read.enabled = true
crm.write.enabled = true
email.draft.enabled = true
```

**Growth** (all Starter flags plus)
```
crm.write.autonomous = true
email.send.supervised.enabled = true
customer.enrich.enabled = true
audit.export.enabled = true
```

**Scale** (all Growth flags plus)
```
email.send.autonomous.enabled = true
customer.pii.enabled = true
agent.multiagent.enabled = true
agent.custom_tools.enabled = true
crm.delete.enabled = true
```

---

## 9. Integration Layer

### 9.1 V1 Integrations

| Integration | Role | Access Level |
|---|---|---|
| **Google Sheets** | Primary data source for ingestion and CRM pipelines | Read-only |
| **HubSpot** | CRM target for contact and deal uploads | Read + gated write |
| **Gmail** | Inbound email parsing; supervised draft sends | Read + supervised send |

### 9.2 Integration Design Principles

- Each integration is implemented as a **versioned adapter**. API changes in the upstream service don't propagate to the agent tool layer.
- All adapters expose a **health-check endpoint** polled every 5 minutes. Failures surface in the dashboard before agents encounter them mid-run.
- **Rate limit budgets** are tracked per deployment. Agents are throttled before hitting external API ceilings.
- Credentials are **stored encrypted** and never exposed to the LLM context, tool definitions, or logs.
- Adapters are the only layer that calls external APIs. The enforcement layer and Agentspan never make outbound integration calls directly.

### 9.3 Post-V1 Integration Roadmap

Salesforce, Pipedrive, Outlook, Airtable, Slack — prioritized by customer demand signals from beta.

---

## 10. User Experience Requirements

### 10.1 Core UX Principles

- A non-technical user must deploy their first agent in **under 10 minutes**.
- Every agent action must be **explainable in plain English** — no technical jargon in user-facing logs.
- The **review queue is the primary trust surface** of the product. It must be fast, clear, and low-friction.
- Failure states must be **specific and actionable**. "Something went wrong" is never acceptable.
- Users must be able to **pause or stop any agent** in one click from any screen.

### 10.2 Core Screens

**Agent Gallery**
Browse available agent types by domain. Cards show what the agent does, what it connects to, and what permissions it needs. One click to begin setup.

**Agent Setup Wizard** *(3 steps)*
1. Connect your accounts (OAuth flows for Sheets, HubSpot, Gmail)
2. Configure task parameters (source, destination, field mappings via UI — no code)
3. Set review preferences (what requires approval, notification channel)

**Review Queue**
Staged agent proposals — CRM records, email drafts, data outputs — displayed with before/after diffs where applicable. Actions: Accept / Edit / Reject. Bulk approve supported. Each item shows which agent produced it and why.

**Activity Dashboard**
Plain-English log of every agent action. Columns: timestamp, agent name, action taken, outcome (success/pending/failed), estimated cost. Filterable by agent, domain, date. One-click drill-down to full step trace (powered by Agentspan observability).

**Agent Settings**
Pause/resume toggle, edit configuration, view active feature flags, tier upgrade prompt, delete agent.

---

## 11. Development Milestones

| Phase | Timeline | Deliverable | Success Criteria |
|---|---|---|---|
| **M1** | Weeks 1–3 | Feature flag schema + tool registry | Flag definitions documented, unit tested |
| **M2** | Weeks 4–6 | Context resolver + tool surface builder | Correct tool list produced per profile |
| **M3** | Weeks 7–8 | Runtime enforcement layer | Zero disallowed calls reach integration layer |
| **M4** | Weeks 9–11 | Agentspan integration + HITL queue wiring | Review queue pause/resume working end-to-end |
| **M5** | Weeks 12–14 | Data Processing agent (CSV → normalized output) | Full pipeline demo, no integration required |
| **M6** | Weeks 15–17 | CRM Upload agent (Sheets → HubSpot, supervised) | End-to-end with real HubSpot sandbox |
| **M7** | Weeks 18–19 | Email Draft agent + Gmail integration | Draft generated, queued for review, sent on approval |
| **M8** | Weeks 20–21 | Activity dashboard + observability layer | Logs visible, plain-English, filterable |
| **M9** | Weeks 22–23 | Growth tier flag unlock + autonomous modes | Tier flags enforced, autonomous CRM write tested |
| **M10** | Weeks 24–26 | Beta launch — 5 SMB pilot customers | NPS ≥ 40, error rate < 2%, < 1 ticket per 100 runs |

---

## 12. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Integration API breakage (HubSpot, Gmail) | High | Medium | Versioned adapters + health checks + degraded-mode fallback |
| Data mapping errors in CRM writes | High | High | Supervised queue for all CRM writes; field diff preview in review UI |
| Autonomous email sending errors | Medium | High | Autonomous send locked to Scale tier; explicit opt-in required |
| PII mishandling | Low | High | PII tools gated behind `customer.pii.enabled` flag + compliance acknowledgment |
| Agent scope creep (tool call outside profile) | Medium | Medium | Two-gate enforcement (surface builder + call-time check) |
| User trust erosion from opaque failures | Medium | High | Plain-English logs mandatory; no generic error messages |
| Agentspan upstream breaking changes | Low | High | Pin SDK version; abstract Agentspan behind droids-infra interface layer |
| Feature flag schema drift over time | Medium | Medium | Flag registry is source of truth; schema versioned and migration-tested |

---

## 13. Open Questions

The following decisions require resolution before or during development:

1. **Flag ownership model** — Are flag profiles defined entirely by droids-infra per tier, or can Growth/Scale users customize flags within guardrails? Custom flag editing adds power-user flexibility but introduces support and safety complexity.

2. **Context dynamism** — Is the tool surface fixed at session start or adjustable mid-run? Dynamic surfaces are more powerful but harder to audit and reason about.

3. **LLM provider strategy** — Lock to Claude API for V1 (Agentspan supports `anthropic/claude-sonnet-4-6` natively), or abstract the provider now for future flexibility?

4. **Tool marketplace** — Post-V1, should third parties be able to register tools against the flag system? Creates a significant moat but requires trust review, SLA infrastructure, and a flag governance model.

5. **Vertical vs. horizontal V1** — Should the product launch with generic SMB agents or target a specific vertical (e.g., real estate, recruiting) with tailored templates? Vertical has better distribution; horizontal has broader TAM.

6. **Agentspan deployment model** — Fully managed by droids-infra in V1 (simplest), or offer self-hosted Agentspan as a Scale-tier option from the start? Data residency requirements from some SMB verticals may force this decision early.

7. **Pricing model validation** — Usage-based vs. seat-based vs. flat-tier pricing needs SMB customer interviews before finalizing. The flag system makes usage-based pricing natural (charge per flag enabled) but may be confusing for non-technical buyers.

---

## 14. Glossary

| Term | Definition |
|---|---|
| **Agent** | An LLM-driven workflow executor scoped to a task domain and constrained by a deployment profile's feature flags |
| **Agentspan** | The open-source execution engine underlying droids-infra, providing durable workflows, HITL, and observability |
| **Capability Profile** | The resolved set of active feature flags for a specific deployment instance |
| **Context Resolver** | The component that maps deployment context (tier, size, domain) to a capability profile |
| **Deployment Profile** | A flat key-value map of active flags for a specific agent deployment; produced by the context resolver |
| **Feature Flag** | A boolean control that enables or disables a specific tool or behavior for a deployment |
| **Tool Registry** | The master catalog of all tools available in droids-infra, each with its flag requirement and risk profile |
| **Tool Surface** | The set of tool definitions passed to Agentspan for a specific agent session — contains only permitted tools |
| **Tool Surface Builder** | The component that reads a deployment profile and produces the permitted tool list for Agentspan |
| **Enforcement Layer** | The runtime call-time check that validates every tool invocation against the deployment profile before execution |
| **Review Queue** | The supervised approval UI where users accept, edit, or reject agent-proposed actions before they reach integrations |
| **HITL** | Human-in-the-loop — Agentspan's mechanism for pausing an agent indefinitely until a human decision is received |
| **Integration Adapter** | A versioned connector to an external service (HubSpot, Gmail, Sheets); the only layer that makes outbound API calls |

---

*droids-infra PRD v1.0 — Internal Use Only*