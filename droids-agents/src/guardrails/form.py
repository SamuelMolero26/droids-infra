"""Form-subteam guardrails.

``pii_in_form_fields`` (INPUT/HUMAN) — attached to ``web_submit``. Content is
the JSON-serialised tool args; the guardrail recursively scans string field
values for SSN / credit-card patterns and pauses for HITL approval on a hit.
"""

from __future__ import annotations

import re

from agentspan.agents import GuardrailResult

from droids_agents.guardrails import parse_json_content

_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "credit_card": re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
}


def _scan_strings(node: object) -> tuple[str, str, str] | None:
    """DFS through nested JSON-like structure. Returns (label, key, value) on hit."""
    if isinstance(node, str):
        for label, pat in _PII_PATTERNS.items():
            if pat.search(node):
                return (label, "", node)
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                for label, pat in _PII_PATTERNS.items():
                    if pat.search(v):
                        return (label, str(k), v)
            else:
                hit = _scan_strings(v)
                if hit is not None:
                    label, key, value = hit
                    return (label, key or str(k), value)
        return None
    if isinstance(node, list):
        for item in node:
            hit = _scan_strings(item)
            if hit is not None:
                return hit
        return None
    return None


def pii_in_form_fields(content: str) -> GuardrailResult:
    """Pause submission if any field value matches SSN / credit-card pattern."""
    parsed = parse_json_content(content)
    if parsed is None:
        return GuardrailResult(
            passed=False,
            message="web_submit args are not a JSON object",
        )
    hit = _scan_strings(parsed)
    if hit is not None:
        label, key, _value = hit
        where = f" in field {key!r}" if key else ""
        return GuardrailResult(
            passed=False,
            message=f"form field contains {label} pattern{where}; requires HITL approval",
        )
    return GuardrailResult(passed=True)
