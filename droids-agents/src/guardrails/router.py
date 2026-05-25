"""Router-stage guardrails. Applied to the Root agent's input prompt."""

from __future__ import annotations

import re

from agentspan.agents import GuardrailResult

# Coarse jailbreak heuristics. Catches the common phrasings; not a substitute
# for a real classifier. Patterns are case-insensitive and tolerant of
# whitespace.
_JAILBREAK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |the )?previous instructions?",
        r"disregard (all |the )?(above|previous|prior)( instructions?| rules?)?",
        r"forget (all |the )?(above|previous|prior)( instructions?| rules?)?",
        r"you are now (a |an )?\w+",
        r"act as (a |an )?(developer|admin|root|jailbroken)",
        r"system prompt:",
        r"\bDAN\b",
    )
)


def no_jailbreak(content: str) -> GuardrailResult:
    """Reject prompts matching common jailbreak / role-override patterns."""
    for pat in _JAILBREAK_PATTERNS:
        m = pat.search(content)
        if m is not None:
            return GuardrailResult(
                passed=False,
                message=f"prompt matches jailbreak pattern: {m.group(0)!r}",
            )
    return GuardrailResult(passed=True)
