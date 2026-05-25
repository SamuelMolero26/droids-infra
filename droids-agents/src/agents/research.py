"""Research Subteam — parallel competitor fan-out.

agentspan ``Agent.name`` is the cache key for the compile step. It must be a
stable identifier across runs of the same shape, so leaf names are
``competitor_0``, ``competitor_1``, … — NOT the random droid names. The droid
identity is surfaced via ``metadata`` so the UI and CLI both see it.

Guardrails attach to each LEAF competitor (single-object logic). The
``research_team`` container has NO guardrails — the per-leaf result is the
right unit of inspection.
"""

from __future__ import annotations

from agentspan.agents import Agent, Guardrail, OnFail, Position, Strategy

from droids_agents.guardrails.research import findings_quality, findings_structural
from droids_agents.naming import NamePool, claim_for_role
from droids_agents.schemas import CompetitorFinding
from droids_agents.tools.playwright import web_extract_text, web_navigate

_MODEL = "anthropic/claude-sonnet-4-6"


def competitor_agent(
    pool: NamePool,
    *,
    competitor: str,
    index: int,
    slice_lines: list[str],
) -> Agent:
    """Build one leaf competitor agent.

    The ``instructions`` closure uses lambda default-arg capture (``s=...``,
    ``c=...``) to avoid Python's late-binding-in-loop trap when the caller
    builds N agents in a list comprehension.
    """
    md = claim_for_role(pool, "competitor")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"

    return Agent(
        name=f"competitor_{index}",
        model=_MODEL,
        instructions=(
            lambda s=slice_block, c=competitor: (
                f"Role: Researcher for {c}.\n"
                f"Prior-run context:\n{s}\n\n"
                "Task: investigate the competitor and emit a single "
                "CompetitorFinding JSON object with non-empty `summary` "
                "(≥50 chars) and an http(s) `source_url`."
            )
        ),
        tools=[web_navigate, web_extract_text],
        output_type=CompetitorFinding,
        metadata=md,
        guardrails=[
            Guardrail(
                findings_structural,
                position=Position.OUTPUT,
                on_fail=OnFail.RETRY,
                max_retries=2,
            ),
            Guardrail(findings_quality, position=Position.OUTPUT, on_fail=OnFail.HUMAN),
        ],
    )


def research_team(
    pool: NamePool,
    *,
    competitors: list[str],
    slice_map: dict[str, list[str]],
) -> Agent:
    """Parallel fan-out container. No guardrails on the container itself."""
    leaves = [
        competitor_agent(
            pool,
            competitor=c,
            index=i,
            slice_lines=slice_map.get(c, []),
        )
        for i, c in enumerate(competitors)
    ]
    return Agent(
        name="research_team",
        model=_MODEL,
        strategy=Strategy.PARALLEL,
        agents=leaves,
    )
