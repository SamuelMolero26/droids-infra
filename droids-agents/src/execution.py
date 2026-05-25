"""Execution preparation — surface-independent.

The CLI (console + exit codes) and the TUI (threaded state + polling) are two
presentation surfaces over the SAME pipeline. Everything between "I have a
prompt" and "I have a Root agent ready to run" lives here so a fix lands once,
not once per surface.

Layers:

- ``plan_execution`` — pure decision logic (classify -> steps -> competitors ->
  validate). No agentspan, no network. Unit-testable with a stub Anthropic
  client. Raises ``ExecutionError`` subclasses the caller renders.
- ``build_execution`` — assembly (mem fetch -> slice_map -> build_root). Needs
  externals; mem fetch is injectable for tests.
- ``interpret_result`` — classify an agentspan result into an ``Outcome`` both
  surfaces render their own way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic
from droids_agents.config import Settings
from droids_agents.naming import NamePool
from droids_agents.router import (
    build_root,
    classify_prompt,
    extract_competitors,
    plan_mixed_steps,
)
from droids_agents.schemas import (
    LABEL_TO_TASK_TYPE,
    ClassifierLabel,
    MemoryLoaderResult,
    TaskType,
    label_to_task_type,
)
from droids_agents.slicing import slice_for
from droids_agents.tools.mem import MemFetchError, fetch_mem_context

# Sub-agent roles per classifier label. Single source of truth for both the
# slice_map (which Slices to cut) and the TUI agent table (what to display).
ROLES_BY_LABEL: dict[str, tuple[str, ...]] = {
    "research": ("competitor",),
    "docs": ("extractor", "synthesizer"),
    "form": ("form_planner", "form_executor"),
    "messaging": ("drafter", "sender"),
}


def roles_for_steps(steps: list[ClassifierLabel]) -> list[str]:
    """Ordered, de-duplicated Sub-agent roles for a step sequence."""
    out: list[str] = []
    seen: set[str] = set()
    for s in steps:
        for r in ROLES_BY_LABEL.get(s, ()):
            if r not in seen:
                seen.add(r)
                out.append(r)
    return out


def build_slice_map(
    *, bundle, prompt: str, steps: list[ClassifierLabel]
) -> dict[str, list[str]]:
    """Map each needed Sub-agent role to its Slice of the Bundle."""
    return {role: slice_for(role, bundle, prompt) for role in roles_for_steps(steps)}


# --- Typed errors (each surface renders these its own way) ----------------


class ExecutionError(Exception):
    """Base for recoverable preparation failures. Carries a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidTaskType(ExecutionError):
    """--task-type override is not a known TaskType."""


class CompetitorsRequired(ExecutionError):
    """Research step selected but no competitors given or extractable."""


class GmailRequired(ExecutionError):
    """Messaging step selected but Gmail is not configured."""


class MemUnreachable(ExecutionError):
    """droids-mem mem_context fetch failed."""


# --- Decision layer (pure, testable) --------------------------------------


@dataclass(frozen=True)
class ExecutionPlan:
    steps: list[ClassifierLabel]
    task_type: TaskType
    competitors: list[str]


def _resolve_steps(
    *, client: Anthropic, prompt: str, task_type_override: str | None
) -> list[ClassifierLabel]:
    """Run classifier (and mixed planner if needed). Honors task_type_override."""
    if task_type_override:
        for label, tt in LABEL_TO_TASK_TYPE.items():
            if tt == task_type_override:
                return [label]  # type: ignore[list-item]
        raise InvalidTaskType(
            "invalid_task_type",
            f"--task-type {task_type_override!r} is not a valid TaskType",
        )
    label = classify_prompt(prompt, client=client)
    if label != "mixed":
        return [label]
    return plan_mixed_steps(prompt, client=client)


def _resolve_competitors(
    *, client: Anthropic, prompt: str, steps: list[ClassifierLabel], given: list[str]
) -> list[str]:
    """When the research step runs with no competitors, extract them from the
    prompt. Raises CompetitorsRequired if none are identifiable."""
    if "research" not in steps or given:
        return given
    extracted = extract_competitors(prompt, client=client)
    if not extracted:
        raise CompetitorsRequired(
            "competitors_required",
            "the research step requires competitor names but none were found in "
            'the prompt. Pass them explicitly with --competitors "Name1,Name2".',
        )
    return extracted


def plan_execution(
    *,
    settings: Settings,
    client: Anthropic,
    prompt: str,
    competitors: list[str],
    task_type_override: str | None,
) -> ExecutionPlan:
    """Classify -> resolve steps -> resolve competitors -> validate. Pure: no
    agentspan, no network. Raises ExecutionError subclasses on bad input."""
    steps = _resolve_steps(
        client=client, prompt=prompt, task_type_override=task_type_override
    )
    task_type = label_to_task_type(steps[0])
    resolved_competitors = _resolve_competitors(
        client=client, prompt=prompt, steps=steps, given=competitors
    )
    if "messaging" in steps and not settings.gmail_enabled:
        raise GmailRequired(
            "gmail_not_configured",
            "this prompt needs the messaging Subteam but Gmail is not configured. "
            "Set GOOGLE_CREDENTIALS_JSON + GOOGLE_TOKEN_JSON and run "
            "`droids-agents auth gmail`, or rephrase to avoid email tasks.",
        )
    return ExecutionPlan(
        steps=steps, task_type=task_type, competitors=resolved_competitors
    )


# --- Assembly layer -------------------------------------------------------


@dataclass
class PreparedExecution:
    plan: ExecutionPlan
    session_id: str
    root: Any  # agentspan Agent
    roles: list[str]


def build_execution(
    *,
    settings: Settings,
    plan: ExecutionPlan,
    prompt: str,
    pool: NamePool,
    docs_basenames: list[str],
    session_id_override: str | None,
    max_total_tokens: int | None,
    fetch_context: Callable[..., MemoryLoaderResult] = fetch_mem_context,
) -> PreparedExecution:
    """Fetch Bundle, cut Slices, build the Root agent. Raises MemUnreachable
    if mem_context is down. ``fetch_context`` is injectable for tests."""
    try:
        mem = fetch_context(settings, task_type=plan.task_type, query=prompt)
    except MemFetchError as e:
        raise MemUnreachable("mem_unreachable", str(e)) from e

    session_id = session_id_override or mem.session_id
    slice_map = build_slice_map(bundle=mem.bundle, prompt=prompt, steps=plan.steps)
    root = build_root(
        settings,
        pool=pool,
        prompt=prompt,
        steps=plan.steps,
        session_id=session_id,
        competitors=plan.competitors,
        docs_basenames=docs_basenames,
        slice_map=slice_map,
        max_total_tokens=max_total_tokens,
    )
    return PreparedExecution(
        plan=plan,
        session_id=session_id,
        root=root,
        roles=roles_for_steps(plan.steps),
    )


# --- Result interpretation (shared) ---------------------------------------


def _result_get(result: Any, *names: str, default: Any = None) -> Any:
    """Tolerate either object-attr or dict-key shapes from agentspan results."""
    for n in names:
        if hasattr(result, n):
            return getattr(result, n)
        if isinstance(result, dict) and n in result:
            return result[n]
    return default


@dataclass
class Outcome:
    kind: str  # "hitl" | "dry_run" | "cost_cap" | "ok"
    exec_id: str
    output: Any = None
    hitl: dict[str, Any] = field(default_factory=dict)
    termination_reason: str = ""


def interpret_result(result: Any, *, dry_run: bool) -> Outcome:
    """Classify an agentspan run result into a surface-independent Outcome."""
    exec_id = _result_get(result, "execution_id", "exec_id", default="<unknown>")

    if _result_get(result, "is_waiting", default=False):
        pending = _result_get(result, "pending_approval", "waiting_for", default={})
        if not isinstance(pending, dict):
            pending = {}
        return Outcome(kind="hitl", exec_id=exec_id, hitl=pending)

    output = _result_get(result, "output", default={})
    if hasattr(output, "model_dump"):
        output = output.model_dump()

    if dry_run:
        return Outcome(kind="dry_run", exec_id=exec_id, output=output)

    termination_reason = _result_get(result, "termination_reason", default="")
    if "token" in str(termination_reason).lower():
        return Outcome(
            kind="cost_cap",
            exec_id=exec_id,
            output=output,
            termination_reason=str(termination_reason),
        )

    return Outcome(kind="ok", exec_id=exec_id, output=output)
