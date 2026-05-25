"""Messaging Subteam — drafter + sender with handoff strategy.

``drafter`` writes the email body purely via LLM, no external tools.
``sender`` holds ``gmail_draft`` and the HITL-gated ``gmail_send``. The
``recipient_allowlist`` INPUT guardrail is a factory closure over the
per-Execution allowlist (``Settings.email_allowlist``) — attached to the
sender agent so it inspects ``gmail_send`` call args.
"""

from __future__ import annotations

from collections.abc import Iterable

from agentspan.agents import Agent, Guardrail, OnFail, Position
from droids_agents.guardrails.messaging import (
    make_recipient_allowlist,
    pii_in_draft,
    tone_length,
)
from droids_agents.naming import NamePool, claim_for_role
from droids_agents.schemas import EmailDraft, EmailSendResult
from droids_agents.tools.gmail import gmail_draft, gmail_send

_MODEL = "anthropic/claude-sonnet-4-6"


def drafter_agent(pool: NamePool, *, slice_lines: list[str]) -> Agent:
    md = claim_for_role(pool, "drafter")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"
    return Agent(
        name="drafter",
        model=_MODEL,
        instructions=(
            lambda s=slice_block: (
                "Role: Email-Drafter. Compose the email body. Emit an "
                "EmailDraft JSON object with `recipient`, `subject`, `body`. "
                "Avoid PII (SSN, credit cards, phone numbers).\n"
                f"Prior-run context:\n{s}"
            )
        ),
        output_type=EmailDraft,
        metadata=md,
        guardrails=[
            Guardrail(pii_in_draft, position=Position.OUTPUT, on_fail=OnFail.HUMAN),
            Guardrail(
                tone_length,
                position=Position.OUTPUT,
                on_fail=OnFail.RETRY,
                max_retries=2,
            ),
        ],
    )


def sender_agent(
    pool: NamePool,
    *,
    slice_lines: list[str],
    email_allowlist: Iterable[str],
) -> Agent:
    md = claim_for_role(pool, "sender")
    slice_block = "\n".join(slice_lines) if slice_lines else "(no prior context)"
    recipient_allowlist = make_recipient_allowlist(email_allowlist)
    return Agent(
        name="sender",
        model=_MODEL,
        instructions=(
            lambda s=slice_block: (
                "Role: Email-Sender. Take the EmailDraft and either save it via "
                "gmail_draft or send via gmail_send (HITL-gated). Emit an "
                "EmailSendResult JSON object.\n"
                f"Prior-run context:\n{s}"
            )
        ),
        tools=[gmail_draft, gmail_send],
        output_type=EmailSendResult,
        metadata=md,
        guardrails=[
            Guardrail(
                recipient_allowlist,
                position=Position.INPUT,
                on_fail=OnFail.HUMAN,
                name="recipient_allowlist",
            ),
        ],
    )


def messaging_team(
    pool: NamePool,
    *,
    slice_map: dict[str, list[str]],
    email_allowlist: Iterable[str],
) -> Agent:
    drafter = drafter_agent(pool, slice_lines=slice_map.get("drafter", []))
    sender = sender_agent(
        pool,
        slice_lines=slice_map.get("sender", []),
        email_allowlist=email_allowlist,
    )
    return Agent(
        name="messaging_team",
        model=_MODEL,
        instructions=(
            "Coordinate the Email-Drafter and Email-Sender: draft first, then "
            "send (or save as draft). Hand off to the sender only once the body "
            "is final."
        ),
        agents=[drafter, sender],
        strategy="handoff",
    )
