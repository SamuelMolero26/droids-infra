"""Router stage + Root composition.

The classifier and mixed-planner run as **plain Anthropic SDK calls** on
``claude-haiku-4-5`` BEFORE the Root is compiled — agentspan builds a static
``WorkflowDef`` up front, so a dynamic ``mixed`` SEQUENTIAL cannot be composed
inside a running Agent.

The CLI flow is:

    label = classify_prompt(prompt)
    if label == "mixed":
        steps = plan_mixed_steps(prompt)
        root = build_root(settings, pool, prompt, steps=steps, ...)
    else:
        root = build_root(settings, pool, prompt, steps=[label], ...)

``build_root`` chooses a stable name per shape (``root_single_research`` /
``root_mixed_research_docs``) so agentspan's compile cache reuses across runs
of the same topology.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from agentspan.agents import (
    Agent,
    Guardrail,
    MaxMessageTermination,
    OnFail,
    Position,
    Strategy,
    TokenUsageTermination,
)
from anthropic import Anthropic
from droids_agents.agents.docs import doc_team
from droids_agents.agents.form import form_team
from droids_agents.agents.messaging import messaging_team
from droids_agents.agents.research import research_team
from droids_agents.config import Settings
from droids_agents.guardrails.router import no_jailbreak
from droids_agents.naming import NamePool, claim_for_role
from droids_agents.schemas import (
    ClassifierLabel,
    MemoryLoaderResult,
    RollupResult,
    TaskType,
    label_to_task_type,
)
from droids_agents.tools.mem import mem_tools

_HAIKU = "claude-haiku-4-5"  
_HAIKU_AGENT = "anthropic/claude-haiku-4-5" 
_ROOT_MAX_TURNS = 40

_CLASSIFY_LABELS: tuple[str, ...] = ("research", "docs", "form", "messaging", "mixed")
_SUBTEAM_LABELS: tuple[str, ...] = ("research", "docs", "form", "messaging")


# --- Pre-compile LLM stages (CLI calls these before build_root) -----------


def make_client(settings: Settings) -> Anthropic:
    return Anthropic(api_key=settings.anthropic_api_key)


def _parse_json_object(raw: str) -> dict:
    """Parse the first JSON object out of an LLM response.

    Haiku frequently wraps JSON in ```json fences or adds prose around it, so
    a bare ``json.loads`` fails. Slice from the first ``{`` to the last ``}``
    before parsing. Raises ValueError if no object is present."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(raw[start : end + 1])


def classify_prompt(prompt: str, *, client: Anthropic) -> ClassifierLabel:
    """Cheap one-token classification using haiku. Falls back to ``mixed``.

    Cost: ~$0.0001 per call.
    """
    sys = (
        "Classify the user's task into exactly ONE of: "
        f"{', '.join(_CLASSIFY_LABELS)}. Reply with ONLY the label, lowercase, "
        "no punctuation, no explanation. Use `mixed` only when the task clearly "
        "spans two or more of the single-label subteams."
    )
    msg = client.messages.create(
        model=_HAIKU,
        max_tokens=8,
        system=sys,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip().lower()
    label = raw.split()[0].strip(".,;:!?\"'`") if raw else "mixed"
    if label not in _CLASSIFY_LABELS:
        label = "mixed"
    return label  # type: ignore[return-value]


def plan_mixed_steps(prompt: str, *, client: Anthropic) -> list[ClassifierLabel]:
    """For ``mixed`` prompts: emit an ordered list of subteam labels (max 4, no dup).

    JSON-shape enforced via prompt. If the response is malformed, falls back to
    ``["research"]`` so the Execution still runs.
    """
    sys = (
        "Decompose the user's task into a SEQUENCE of subteam steps. Choose only "
        f"from: {', '.join(_SUBTEAM_LABELS)}. Return STRICT JSON with a single "
        "key `steps` whose value is an array of 1-4 unique labels in execution "
        "order. Example: {\"steps\": [\"research\", \"docs\"]}."
    )
    msg = client.messages.create(
        model=_HAIKU,
        max_tokens=128,
        system=sys,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    try:
        data = _parse_json_object(raw)
        steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(steps, list) or not steps:
            return ["research"]
        seen: set[str] = set()
        out: list[ClassifierLabel] = []
        for s in steps:
            if not isinstance(s, str):
                continue
            label = s.strip().lower()
            if label in _SUBTEAM_LABELS and label not in seen:
                seen.add(label)
                out.append(label)  # type: ignore[arg-type]
            if len(out) >= 4:
                break
        return out or ["research"]
    except (ValueError, TypeError):
        return ["research"]


def extract_competitors(prompt: str, *, client: Anthropic) -> list[str]:
    """Extract company/product names to research from the prompt.

    Returns empty list if none are identifiable — caller must handle that case.
    Cost: ~$0.0001 per call.
    """
    sys = (
        "Extract the names of companies, products, or services the user wants to research. "
        "Return STRICT JSON with a single key `competitors` whose value is an array of name strings. "
        "Preserve original casing. If none are identifiable, return {\"competitors\": []}. "
        "Example: \"Compare OpenAI and Anthropic APIs\" → {\"competitors\": [\"OpenAI\", \"Anthropic\"]}."
    )
    msg = client.messages.create(
        model=_HAIKU,
        max_tokens=128,
        system=sys,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
    try:
        data = _parse_json_object(raw)
        names = data.get("competitors") if isinstance(data, dict) else None
        if not isinstance(names, list):
            return []
        return [n.strip() for n in names if isinstance(n, str) and n.strip()]
    except (ValueError, TypeError):
        return []


# --- Root sub-step Agents (memory_loader, rollup) ------------------------


def memory_loader_agent(
    pool: NamePool,
    *,
    settings: Settings,
    task_type: TaskType,
    prompt: str,
) -> Agent:
    """Calls ``mem_context(task_type, query)`` and unwraps into MemoryLoaderResult.

    Reads ``session_id`` from the MCP envelope's top-level field, NOT from
    inside ``bundle``.
    """
    md = claim_for_role(pool, "memory_loader")
    return Agent(
        name="memory_loader",
        model=_HAIKU_AGENT,
        instructions=(
            lambda tt=task_type, q=prompt: (
                "Role: Memory-Loader. Call mem_context with "
                f"task_type='{tt}' and query='{q}'. The MCP response envelope "
                "is {{session_id, context}}. Read `session_id` from the "
                "TOP-LEVEL field. Re-package the result as a MemoryLoaderResult "
                "JSON object with the unwrapped context as `bundle`."
            )
        ),
        tools=mem_tools(settings),
        output_type=MemoryLoaderResult,
        metadata=md,
    )


def rollup_agent(
    pool: NamePool,
    *,
    settings: Settings,
    task_type: TaskType,
    session_id: str,
) -> Agent:
    """Sole droids-mem writer. Composes RollupResult from typed sub-outputs.

    ``session_id`` is baked into instructions so every ``mem_save`` shares it.
    """
    md = claim_for_role(pool, "rollup")
    return Agent(
        name="rollup",
        model=_HAIKU_AGENT,
        instructions=(
            lambda tt=task_type, sid=session_id: (
                "Role: Rollup. Compose a RollupResult JSON object from the "
                "preceding Subteam outputs:\n"
                f"- `summary` (SessionSummary, task_type='{tt}') — REQUIRED. "
                "Include an aggregate cost line in `what` like `Cost: $X / N tok`.\n"
                "- `new_patterns` (≤3) — only reusable URLs/selectors/format recipes.\n"
                "- `new_errors` (≤3) — only failure modes worth recalling.\n"
                "- `new_rules` (≤2) — only explicit durable preferences from HITL edits.\n"
                f"After emitting, call mem_save once per row with session_id='{sid}'."
            )
        ),
        tools=mem_tools(settings),
        output_type=RollupResult,
        metadata=md,
    )


# --- Root composition ----------------------------------------------------


def _subteam_for(
    label: ClassifierLabel,
    *,
    pool: NamePool,
    competitors: list[str],
    docs_basenames: list[str],
    email_allowlist: Iterable[str],
    slice_map: dict[str, list[str]],
) -> Agent:
    """Pick the Subteam factory for one classifier label."""
    if label == "research":
        return research_team(pool, competitors=competitors, slice_map=slice_map)
    if label == "docs":
        return doc_team(pool, slice_map=slice_map, docs_basenames=docs_basenames)
    if label == "form":
        return form_team(pool, slice_map=slice_map)
    if label == "messaging":
        return messaging_team(
            pool, slice_map=slice_map, email_allowlist=email_allowlist
        )
    raise ValueError(f"cannot build subteam for label {label!r}")


def _root_name(steps: list[ClassifierLabel]) -> str:
    """Stable cache key: ``root_single_research`` / ``root_mixed_research_docs``."""
    if len(steps) == 1:
        return f"root_single_{steps[0]}"
    return "root_mixed_" + "_".join(steps)


def build_root(
    settings: Settings,
    *,
    pool: NamePool,
    prompt: str,
    steps: list[ClassifierLabel],
    session_id: str,
    competitors: list[str] | None = None,
    docs_basenames: list[str] | None = None,
    slice_map: dict[str, list[str]] | None = None,
    max_total_tokens: int | None = None,
) -> Agent:
    """Compose Root = SEQUENTIAL([*<each step's Subteam>, rollup]).

    V1 deviation from plan: the ``memory_loader`` Agent is NOT inside Root.
    agentspan compiles a static workflow, so the Bundle must exist at build
    time for Sub-agent factories to bake slices. The CLI calls
    ``tools.mem.fetch_mem_context`` directly before this function, threads the
    minted ``session_id`` here, and passes the sliced ``slice_map`` in.

    Sub-agent factories take ``slice_map`` keyed by role.
    """
    if not steps:
        raise ValueError("steps must be non-empty")
    if not session_id:
        raise ValueError("session_id is required (call fetch_mem_context first)")
    task_type = label_to_task_type(steps[0])
    competitors = competitors or []
    docs_basenames = docs_basenames or []
    slice_map = slice_map or {}

    subteams = [
        _subteam_for(
            s,
            pool=pool,
            competitors=competitors,
            docs_basenames=docs_basenames,
            email_allowlist=settings.email_allowlist,
            slice_map=slice_map,
        )
        for s in steps
    ]

    roll = rollup_agent(
        pool, settings=settings, task_type=task_type, session_id=session_id
    )

    termination = MaxMessageTermination(_ROOT_MAX_TURNS)
    if max_total_tokens is not None:
        termination = termination | TokenUsageTermination(
            max_total_tokens=max_total_tokens
        )

    return Agent(
        name=_root_name(steps),
        model=_HAIKU_AGENT,
        instructions=(
            "Root coordinator. Run each Subteam in order, then rollup. Pass "
            "structured outputs forward; do not summarise in prose."
        ),
        agents=[*subteams, roll],
        strategy=Strategy.SEQUENTIAL,
        termination=termination,
        guardrails=[
            Guardrail(no_jailbreak, position=Position.INPUT, on_fail=OnFail.RAISE),
        ],
    )
