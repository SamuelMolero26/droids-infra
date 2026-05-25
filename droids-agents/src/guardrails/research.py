"""Research-subteam guardrails. Attached to each LEAF competitor agent.

Layered: ``findings_structural`` (RETRY x2) → cheap structural check;
``findings_quality`` (HUMAN) → stricter substantive check. agentspan runs
guardrails in order — first failure wins. The container ``research_team``
has NO guardrails (single-object logic doesn't fit an aggregated list).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from agentspan.agents import GuardrailResult
from droids_agents.guardrails import parse_json_content

_MIN_SUMMARY_CHARS: int = 50

_APOLOGY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bi couldn'?t find\b",
        r"\bi (do not|don'?t) have access\b",
        r"\bas an ai\b",
        r"\bi'?m unable to\b",
        r"\bno (information|data) (is )?available\b",
    )
)


def _extract_finding(content: str) -> dict | None:
    """Pull a CompetitorFinding-shaped dict out of content. None if absent."""
    parsed = parse_json_content(content)
    if parsed is None:
        return None
    # If the agent returned a list of findings, accept first; otherwise direct.
    if isinstance(parsed, dict) and "summary" in parsed and "source_url" in parsed:
        return parsed
    return None


def findings_structural(content: str) -> GuardrailResult:
    """Each CompetitorFinding has non-empty summary AND source_url fields."""
    finding = _extract_finding(content)
    if finding is None:
        return GuardrailResult(
            passed=False,
            message="output is not a CompetitorFinding JSON object",
        )
    summary = (finding.get("summary") or "").strip()
    source_url = (finding.get("source_url") or "").strip()
    if not summary:
        return GuardrailResult(passed=False, message="`summary` is empty")
    if not source_url:
        return GuardrailResult(passed=False, message="`source_url` is empty")
    return GuardrailResult(passed=True)


def findings_quality(content: str) -> GuardrailResult:
    """Stricter substantive checks. Runs after structural retries are spent."""
    finding = _extract_finding(content)
    if finding is None:
        return GuardrailResult(
            passed=False,
            message="output is not a CompetitorFinding JSON object",
        )
    summary = (finding.get("summary") or "").strip()
    source_url = (finding.get("source_url") or "").strip()

    parsed = urlparse(source_url)
    if parsed.scheme not in ("http", "https"):
        return GuardrailResult(
            passed=False,
            message=f"`source_url` scheme must be http(s), got {parsed.scheme!r}",
        )
    if len(summary) < _MIN_SUMMARY_CHARS:
        return GuardrailResult(
            passed=False,
            message=f"`summary` is too short ({len(summary)} < {_MIN_SUMMARY_CHARS} chars)",
        )
    for pat in _APOLOGY_PATTERNS:
        m = pat.search(summary)
        if m is not None:
            return GuardrailResult(
                passed=False,
                message=f"summary contains apology pattern: {m.group(0)!r}",
            )
    return GuardrailResult(passed=True)
