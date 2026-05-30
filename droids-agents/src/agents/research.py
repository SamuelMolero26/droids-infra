"""Research Subteam — parallel competitor fan-out.

agentspan ``Agent.name`` is the cache key for the compile step. It must be a
stable identifier across runs of the same shape, so leaf names are
``competitor_0``, ``competitor_1``, … — NOT the random droid names. The droid
identity is surfaced via ``metadata`` so the UI and CLI both see it.

No OUTPUT guardrails attach to leaves: agentspan fires output guardrails after
every LLM turn (including planning prose alongside tool calls), so a final-
shape check would false-trip on intermediates. ``output_type=CompetitorFinding``
plus on-schema Pydantic validators (length, scheme, apology) enforce structure
and quality at the only correct boundary — final structured emit. The
``research_team`` container also has no guardrails.
"""

from __future__ import annotations

from agentspan.agents import Agent, Strategy
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
                "(≥50 chars) and an http(s) `source_url`.\n\n"
                "Tool-budget: at most ONE successful web_navigate + ONE "
                "web_extract_text. Stop after extracting text and emit the "
                "CompetitorFinding immediately.\n\n"
                "HARD STOP RULES — these override everything else:\n"
                "1. Count every web_navigate and every web_extract_text. Stop "
                "using tools after a total of 4 tool calls, regardless of outcome.\n"
                "2. If web_extract_text returns `ok: false` (including "
                "`no open page`, `empty extraction`, or anti-bot block), DO NOT "
                "call web_extract_text again, and DO NOT re-navigate to the same "
                "URL. Either try ONE alternative URL once, or stop tool use.\n"
                "3. The moment any STOP rule fires, immediately emit a "
                "CompetitorFinding using the last URL you successfully navigated "
                "to as `source_url` (or the last URL you attempted if no "
                "navigation succeeded), and write a `summary` that combines what "
                "any successful extraction showed with publicly-known facts "
                f"about {c}. Add a short `notes` line explaining the tool "
                "outcome (e.g. \"extract failed: no open page\").\n"
                "4. NEVER emit empty text and NEVER end your turn without a "
                "CompetitorFinding JSON. Best-effort with a real URL beats "
                "silence.\n\n"
                "IMPORTANT: Do NOT output any explanatory or planning text. "
                "Your only text output must be the final CompetitorFinding JSON."
            )
        ),
        tools=[web_navigate, web_extract_text],
        output_type=CompetitorFinding,
        metadata=md,
        max_turns=6,
        max_tokens=1024,
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
            slice_lines=slice_map.get("competitor", []),
        )
        for i, c in enumerate(competitors)
    ]
    return Agent(
        name="research_team",
        model=_MODEL,
        strategy=Strategy.PARALLEL,
        agents=leaves,
    )
