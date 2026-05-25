"""Pydantic schemas for structured Sub-agent output and droids-mem writes.

The Root agent composes the Rollup from typed Sub-agent outputs — no LLM
parsing of prose. Guardrails inspect the same structured fields. droids-mem
writes use the per-kind payload models so every `mem_save` carries the three
required strings (``title``, ``what``, ``learned``) plus ``task_type``.

Field semantics:
- ``title``    — short label (≤ ~80 chars).
- ``what``     — context / what happened. Excluded from the fingerprint by
                 design (ADR 0001).
- ``learned``  — distilled, reusable takeaway. Part of fingerprint.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --- Classifier vocab ----------------------------------------------------

ClassifierLabel = Literal["research", "docs", "form", "messaging", "mixed"]
"""Primary router output. ``mixed`` triggers a second LLM call (mixed_planner)."""

TaskType = Literal[
    "competitor_research",
    "doc_synthesis",
    "form_submission",
    "email_messaging",
]
"""Fixed V1 vocab. Slicing rules, retention bounds, and `mem_context(task_type=...)`
filters all match against these constants.
"""

LABEL_TO_TASK_TYPE: dict[ClassifierLabel, TaskType] = {
    "research": "competitor_research",
    "docs": "doc_synthesis",
    "form": "form_submission",
    "messaging": "email_messaging",
    # "mixed" is handled per-step by the mixed_planner — no direct mapping.
}


def label_to_task_type(label: ClassifierLabel) -> TaskType:
    if label == "mixed":
        raise ValueError("mixed must be expanded into per-step labels first")
    return LABEL_TO_TASK_TYPE[label]


# --- droids-mem read envelope -------------------------------------------


class ContextMemory(BaseModel):
    """One row inside a droids-mem ContextResponse.

    Tier-aware: ``learned`` is populated for ``always`` tier; ``snippet`` for
    ``browse`` tier. Reading the wrong field for the tier silently injects
    nothing — slicing code MUST select per ``tier``.
    """

    id: str
    kind: str
    task_type: str
    title: str
    tier: Literal["always", "browse"]
    learned: str = ""
    snippet: str = ""
    created_at: int | None = None


class ContextResponse(BaseModel):
    """Bundle returned by ``mem_context`` (minus the top-level ``session_id``)."""

    task_type: str
    last_session: ContextMemory | None = None
    user_rules: list[ContextMemory] = Field(default_factory=list)
    browse: list[ContextMemory] = Field(default_factory=list)


class MemoryLoaderResult(BaseModel):
    """Output of the Root's ``memory_loader`` step.

    ``session_id`` is read from the MCP envelope's top-level field, NOT from
    inside ``bundle``. ``bundle`` is the ``ContextResponse`` unwrapped from the
    ``context`` key.
    """

    session_id: str
    task_type: TaskType
    bundle: ContextResponse


# --- Sub-agent outputs --------------------------------------------------


class CompetitorFinding(BaseModel):
    competitor: str
    summary: str
    source_url: str
    notes: str | None = None


class DocSynthesis(BaseModel):
    synthesis: str
    cited_sources: list[str] = Field(default_factory=list)


class FormPlan(BaseModel):
    url: str
    fields: dict[str, str] = Field(default_factory=dict)
    rationale: str


class FormSubmitResult(BaseModel):
    success: bool
    response_url: str | None = None
    error: str | None = None


class EmailDraft(BaseModel):
    recipient: str
    subject: str
    body: str


class EmailSendResult(BaseModel):
    message_id: str | None = None
    error: str | None = None


# --- droids-mem write payloads ------------------------------------------


class _MemoryWritePayload(BaseModel):
    """Shared body for the four kinds. Title/what/learned all required."""

    task_type: TaskType
    title: str = Field(min_length=1)
    what: str = Field(min_length=1)
    learned: str = Field(min_length=1)
    tags: str = ""


class SessionSummary(_MemoryWritePayload):
    """Always one per Execution. Body should include the aggregate cost line."""


class TaskPattern(_MemoryWritePayload):
    """Reusable recipe (URL / selector / format)."""


class ErrorRecord(_MemoryWritePayload):
    """Failure mode worth recalling. Kind = ``error_resolution`` on the wire."""


class UserRule(_MemoryWritePayload):
    """Durable preference from explicit user / HITL directive."""


# --- Rollup composite ----------------------------------------------------


class RollupResult(BaseModel):
    """Composite Rollup output. Root iterates and writes each row to droids-mem.

    Bounds: ≤3 patterns, ≤3 errors, ≤2 rules per Rollup.
    """

    summary: SessionSummary
    new_patterns: list[TaskPattern] = Field(default_factory=list, max_length=3)
    new_errors: list[ErrorRecord] = Field(default_factory=list, max_length=3)
    new_rules: list[UserRule] = Field(default_factory=list, max_length=2)


# Role keys used by slicing.py and the naming.ROLE_LABELS map.
Role = Literal[
    "competitor",
    "extractor",
    "synthesizer",
    "form_planner",
    "form_executor",
    "drafter",
    "sender",
    "memory_loader",
    "rollup",
]
