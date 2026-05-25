"""Messaging-subteam guardrails.

- ``pii_in_draft`` (OUTPUT/HUMAN) — regex SSN / credit card / phone in body.
- ``recipient_allowlist`` (INPUT/HUMAN) — recipient domain must be in
  ``Settings.email_allowlist``. Empty allowlist = every send pauses for HITL.
- ``tone_length`` (OUTPUT/RETRY) — body length cap + profanity blocklist.

The allowlist guardrail is a factory closed over the per-Execution allowlist.
PII / tone guards are pure module-level functions (stateless).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from agentspan.agents import GuardrailResult
from droids_agents.guardrails import parse_json_content

_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "credit_card": re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
}

_BODY_WORD_CAP: int = 1000

_PROFANITY: frozenset[str] = frozenset(
    {
        # Intentionally short / generic. The CLI can extend via env var in V2.
        "damn",
        "hell",
        "shit",
        "fuck",
    }
)

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _extract_email_body(content: str) -> str | None:
    """Pull EmailDraft.body from JSON; fall back to raw content if not JSON."""
    parsed = parse_json_content(content)
    if parsed is not None and "body" in parsed:
        body = parsed.get("body")
        return body if isinstance(body, str) else None
    return content if isinstance(content, str) else None


def pii_in_draft(content: str) -> GuardrailResult:
    """Reject drafts containing SSN, credit card numbers, or phone numbers."""
    body = _extract_email_body(content) or ""
    for label, pat in _PII_PATTERNS.items():
        m = pat.search(body)
        if m is not None:
            return GuardrailResult(
                passed=False,
                message=f"email body contains {label} pattern: {m.group(0)!r}",
            )
    return GuardrailResult(passed=True)


def tone_length(content: str) -> GuardrailResult:
    """Cap body length at 1000 words; reject profanity blocklist hits."""
    body = _extract_email_body(content) or ""
    words = _WORD_RE.findall(body)
    if len(words) > _BODY_WORD_CAP:
        return GuardrailResult(
            passed=False,
            message=f"email body too long ({len(words)} > {_BODY_WORD_CAP} words)",
        )
    lower = {w.lower() for w in words}
    hits = sorted(lower & _PROFANITY)
    if hits:
        return GuardrailResult(
            passed=False,
            message=f"email body contains profanity: {hits!r}",
        )
    return GuardrailResult(passed=True)


def _recipient_domain(addr: str) -> str | None:
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].strip().lower()


def make_recipient_allowlist(
    allowlist: Iterable[str],
) -> Callable[[str], GuardrailResult]:
    """Factory: close the allowlist guardrail over Settings.email_allowlist.

    The guardrail is attached as an INPUT guard on ``gmail_send``. ``content``
    here is the JSON-serialised tool args (``{"to": ..., "subject": ..., "body": ...}``).
    Empty allowlist = every send pauses for HITL (safe default).
    """
    domains = frozenset(d.strip().lower() for d in allowlist if d and d.strip())

    def recipient_allowlist(content: str) -> GuardrailResult:
        parsed = parse_json_content(content)
        if parsed is None:
            return GuardrailResult(
                passed=False,
                message="gmail_send args are not a JSON object",
            )
        to = parsed.get("to")
        if not isinstance(to, str) or not to.strip():
            return GuardrailResult(
                passed=False, message="`to` is missing or empty"
            )
        if not domains:
            return GuardrailResult(
                passed=False,
                message=(
                    "email allowlist is empty; recipient requires explicit HITL approval"
                ),
            )
        domain = _recipient_domain(to)
        if domain is None:
            return GuardrailResult(
                passed=False, message=f"recipient {to!r} is not a valid email address"
            )
        if domain not in domains:
            return GuardrailResult(
                passed=False,
                message=(
                    f"recipient domain {domain!r} not in allowlist "
                    f"({sorted(domains)}); approve via HITL"
                ),
            )
        return GuardrailResult(passed=True)

    recipient_allowlist.__name__ = "recipient_allowlist"
    return recipient_allowlist
