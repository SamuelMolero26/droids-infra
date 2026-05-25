"""Docs Subteam — extractor + synthesizer swarm.

The extractor pulls text from local docs and web pages; the synthesizer fuses
extracts into a cited synthesis. The two swap control via ``HANDOFF_TO_SYNTH``
/ ``HANDOFF_TO_EXTRACTOR`` mentions, bounded by ``max_turns=10`` on the
container.

Cost: worst-case (max_turns × ``citations_structural`` RETRY x2) ≈ 30 LLM
calls per Execution — set ``--max-cost-usd`` accordingly for large prompts.

``citations_resolve`` is a factory closure over the per-Execution ``--docs``
basename set; the CLI builds the basename frozenset and passes it in.
"""

from __future__ import annotations

from agentspan.agents import (
    Agent,
    Guardrail,
    MaxMessageTermination,
    OnFail,
    Position,
)
from agentspan.agents.handoff import OnTextMention

from droids_agents.guardrails.docs import citations_structural, make_citations_resolve
from droids_agents.naming import NamePool, claim_for_role
from droids_agents.schemas import DocSynthesis
from droids_agents.tools.files import read_doc
from droids_agents.tools.playwright import web_extract_text

_MODEL = "anthropic/claude-sonnet-4-6"
_SWARM_MAX_TURNS = 10


def extractor_agent(pool: NamePool, *, slice_lines: list[str]) -> Agent:
    md = claim_for_role(pool, "extractor")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"
    return Agent(
        name="extractor",
        model=_MODEL,
        instructions=(
            lambda s=slice_block: (
                "Role: Doc-Extractor. Pull raw text from the listed docs and "
                "any necessary web pages. When you have enough material, say "
                "HANDOFF_TO_SYNTH.\n"
                f"Prior-run context:\n{s}"
            )
        ),
        tools=[read_doc, web_extract_text],
        metadata=md,
    )


def synthesizer_agent(
    pool: NamePool,
    *,
    slice_lines: list[str],
    docs_basenames: list[str],
) -> Agent:
    """Synthesizer is pure-LLM (no tools). Citations guardrails attach here."""
    md = claim_for_role(pool, "synthesizer")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"
    citations_resolve = make_citations_resolve(docs_basenames)
    return Agent(
        name="synthesizer",
        model=_MODEL,
        instructions=(
            lambda s=slice_block, b=tuple(docs_basenames): (
                "Role: Doc-Synth. Fuse the Doc-Extractor's material into a "
                "DocSynthesis JSON object. Every factual paragraph MUST end with "
                "a `[source: <basename>]` marker pointing to one of the allowed "
                f"sources: {list(b)}. Say HANDOFF_TO_EXTRACTOR if you need more "
                "material.\n"
                f"Prior-run context:\n{s}"
            )
        ),
        output_type=DocSynthesis,
        metadata=md,
        guardrails=[
            Guardrail(
                citations_structural,
                position=Position.OUTPUT,
                on_fail=OnFail.RETRY,
                max_retries=2,
            ),
            Guardrail(
                citations_resolve,
                position=Position.OUTPUT,
                on_fail=OnFail.HUMAN,
                name="citations_resolve",
            ),
        ],
    )


def doc_team(
    pool: NamePool,
    *,
    slice_map: dict[str, list[str]],
    docs_basenames: list[str],
) -> Agent:
    """Swarm pairing extractor + synthesizer with bounded handoff turns."""
    ex = extractor_agent(pool, slice_lines=slice_map.get("extractor", []))
    sy = synthesizer_agent(
        pool,
        slice_lines=slice_map.get("synthesizer", []),
        docs_basenames=docs_basenames,
    )
    return Agent(
        name="doc_team",
        model=_MODEL,
        strategy="swarm",
        agents=[ex, sy],
        handoffs=[
            OnTextMention(text="HANDOFF_TO_SYNTH", target="synthesizer"),
            OnTextMention(text="HANDOFF_TO_EXTRACTOR", target="extractor"),
        ],
        termination=MaxMessageTermination(_SWARM_MAX_TURNS),
    )
