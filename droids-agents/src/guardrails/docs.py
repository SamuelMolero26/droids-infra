"""Doc-synth guardrails. Attached to the LEAF synthesizer agent.

Layered:
- ``citations_structural`` (RETRY x2) — every factual paragraph has at least
  one ``[source: <basename>]`` marker.
- ``citations_resolve`` (HUMAN) — every ``cited_sources`` basename appears in
  the ``--docs`` set for this Execution (no hallucinated sources).

The Execution-scoped basename set is injected via a factory: the CLI builds
the basename frozenset once when ``--docs`` is parsed and closes the resolve
guardrail over it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from agentspan.agents import GuardrailResult

from droids_agents.guardrails import parse_json_content

# [source: filename.pdf]  /  [source: notes.md]  etc.
_CITATION_MARKER_RE = re.compile(r"\[source:\s*([^\]]+?)\s*\]", re.IGNORECASE)

# A "factual paragraph" = non-empty block separated by blank lines, with at
# least one ASCII word boundary (avoids matching pure-whitespace blocks).
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")


def _extract_synthesis(content: str) -> dict | None:
    parsed = parse_json_content(content)
    if parsed is None:
        return None
    if "synthesis" in parsed and "cited_sources" in parsed:
        return parsed
    return None


def _factual_paragraphs(text: str) -> list[str]:
    """Split synthesis text into paragraphs and drop empty/whitespace-only ones."""
    return [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(text) if p.strip()]


def citations_structural(content: str) -> GuardrailResult:
    """Every factual paragraph contains ``[source: <basename>]``."""
    synth = _extract_synthesis(content)
    if synth is None:
        return GuardrailResult(
            passed=False,
            message="output is not a DocSynthesis JSON object",
        )
    text = synth.get("synthesis") or ""
    paragraphs = _factual_paragraphs(text)
    if not paragraphs:
        return GuardrailResult(passed=False, message="`synthesis` is empty")
    for i, para in enumerate(paragraphs):
        if not _CITATION_MARKER_RE.search(para):
            return GuardrailResult(
                passed=False,
                message=(
                    f"paragraph {i + 1} is missing a [source: ...] citation marker"
                ),
            )
    return GuardrailResult(passed=True)


def make_citations_resolve(docs_basenames: Iterable[str]) -> Callable[[str], GuardrailResult]:
    """Factory: close the resolve guardrail over the Execution's docs set."""
    allowed = frozenset(b.strip() for b in docs_basenames if b and b.strip())

    def citations_resolve(content: str) -> GuardrailResult:
        synth = _extract_synthesis(content)
        if synth is None:
            return GuardrailResult(
                passed=False,
                message="output is not a DocSynthesis JSON object",
            )
        cited = synth.get("cited_sources") or []
        if not isinstance(cited, list):
            return GuardrailResult(
                passed=False, message="`cited_sources` must be a list"
            )
        for ref in cited:
            basename = (str(ref) or "").strip()
            if not basename:
                return GuardrailResult(
                    passed=False, message="`cited_sources` contains an empty entry"
                )
            if basename not in allowed:
                return GuardrailResult(
                    passed=False,
                    message=(
                        f"cited source {basename!r} not in --docs set ({sorted(allowed)})"
                    ),
                )
        # Also check in-text [source: ...] markers resolve to known basenames.
        text = synth.get("synthesis") or ""
        for m in _CITATION_MARKER_RE.finditer(text):
            basename = m.group(1).strip()
            if basename not in allowed:
                return GuardrailResult(
                    passed=False,
                    message=f"in-text citation {basename!r} not in --docs set",
                )
        return GuardrailResult(passed=True)

    citations_resolve.__name__ = "citations_resolve"
    return citations_resolve
