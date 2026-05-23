"""Form Subteam — planner + executor with handoff strategy.

``form_planner`` navigates + fills, emits a ``FormPlan`` JSON object. The
parent (handoff strategy) decides when to swap to ``form_executor``, which
holds the HITL-gated ``web_submit`` tool.

``pii_in_form_fields`` is attached as an INPUT guardrail directly on
``web_submit`` via the tool decorator — *not* on the executor agent — so the
check inspects the actual submission args.
"""

from __future__ import annotations

from agentspan.agents import Agent

from droids_agents.naming import NamePool, claim_for_role
from droids_agents.schemas import FormPlan, FormSubmitResult
from droids_agents.tools.playwright import (
    web_click,
    web_extract_text,
    web_fill,
    web_navigate,
    web_submit,
)

_MODEL = "anthropic/claude-sonnet-4-6"


def form_planner_agent(pool: NamePool, *, slice_lines: list[str]) -> Agent:
    md = claim_for_role(pool, "form_planner")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"
    return Agent(
        name="form_planner",
        model=_MODEL,
        instructions=(
            lambda s=slice_block: (
                "Role: Form-Planner. Navigate to the target page, identify the "
                "form, and emit a FormPlan JSON object with `url`, `fields` "
                "(selector → value), and `rationale`. DO NOT submit.\n"
                f"Prior-run context:\n{s}"
            )
        ),
        tools=[web_navigate, web_extract_text, web_fill, web_click],
        output_type=FormPlan,
        metadata=md,
    )


def form_executor_agent(pool: NamePool, *, slice_lines: list[str]) -> Agent:
    md = claim_for_role(pool, "form_executor")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"
    return Agent(
        name="form_executor",
        model=_MODEL,
        instructions=(
            lambda s=slice_block: (
                "Role: Form-Executor. Take the FormPlan, apply any final field "
                "tweaks via web_fill, then call web_submit. Emit a "
                "FormSubmitResult JSON object capturing `success`, "
                "`response_url`, and `error` if any.\n"
                f"Prior-run context:\n{s}"
            )
        ),
        tools=[web_fill, web_click, web_submit],
        output_type=FormSubmitResult,
        metadata=md,
    )


def form_team(pool: NamePool, *, slice_map: dict[str, list[str]]) -> Agent:
    planner = form_planner_agent(pool, slice_lines=slice_map.get("form_planner", []))
    executor = form_executor_agent(pool, slice_lines=slice_map.get("form_executor", []))
    return Agent(
        name="form_team",
        model=_MODEL,
        instructions=(
            "Coordinate the Form-Planner and Form-Executor: plan first, then "
            "execute. Hand off to the executor only once the plan is complete."
        ),
        agents=[planner, executor],
        strategy="handoff",
    )
