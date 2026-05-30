"""Pydantic schema invariants."""

from __future__ import annotations

import pytest
from droids_agents import schemas
from pydantic import ValidationError


def test_label_to_task_type_known_labels() -> None:
    assert schemas.label_to_task_type("research") == "competitor_research"
    assert schemas.label_to_task_type("docs") == "doc_synthesis"
    assert schemas.label_to_task_type("form") == "form_submission"
    assert schemas.label_to_task_type("messaging") == "email_messaging"


def test_label_to_task_type_mixed_raises() -> None:
    with pytest.raises(ValueError):
        schemas.label_to_task_type("mixed")


@pytest.mark.parametrize(
    "model_cls",
    [
        schemas.SessionSummary,
        schemas.TaskPattern,
        schemas.ErrorRecord,
        schemas.UserRule,
    ],
)
def test_memory_write_payloads_require_nonempty_strings(model_cls) -> None:
    with pytest.raises(ValidationError):
        model_cls(task_type="competitor_research", title="", what="x", learned="y")
    with pytest.raises(ValidationError):
        model_cls(task_type="competitor_research", title="x", what="", learned="y")
    with pytest.raises(ValidationError):
        model_cls(task_type="competitor_research", title="x", what="y", learned="")


def _summary() -> schemas.SessionSummary:
    return schemas.SessionSummary(
        task_type="competitor_research", title="t", what="w", learned="l"
    )


def _pattern() -> schemas.TaskPattern:
    return schemas.TaskPattern(
        task_type="competitor_research", title="t", what="w", learned="l"
    )


def test_rollup_result_max_patterns_bound() -> None:
    with pytest.raises(ValidationError):
        schemas.RollupResult(summary=_summary(), new_patterns=[_pattern()] * 4)


def test_rollup_result_max_rules_bound() -> None:
    rule = schemas.UserRule(
        task_type="competitor_research", title="t", what="w", learned="l"
    )
    with pytest.raises(ValidationError):
        schemas.RollupResult(summary=_summary(), new_rules=[rule] * 3)


def test_memory_loader_result_round_trip() -> None:
    bundle = schemas.ContextResponse(task_type="competitor_research")
    mlr = schemas.MemoryLoaderResult(
        session_id="sess_01ABC", task_type="competitor_research", bundle=bundle
    )
    again = schemas.MemoryLoaderResult.model_validate_json(mlr.model_dump_json())
    assert again.session_id == "sess_01ABC"
    assert again.task_type == "competitor_research"


def test_context_memory_tier_discriminator_enforced() -> None:
    with pytest.raises(ValidationError):
        schemas.ContextMemory(
            id="x", kind="task_pattern", task_type="x", title="x", tier="weird"
        )


# --- CompetitorFinding validators ----------------------------------------
# Length / scheme / apology rules used to live in guardrails/research as an
# OUTPUT guardrail. That fired per LLM turn (including planning prose) and
# false-tripped HITL. Rules now live on the schema and run only at structured-
# output parse time.


def _finding(**overrides):
    base = dict(
        competitor="Acme",
        summary="x" * 60,
        source_url="https://example.com",
    )
    base.update(overrides)
    return base


def test_competitor_finding_accepts_clean() -> None:
    cf = schemas.CompetitorFinding(**_finding())
    assert cf.summary == "x" * 60
    assert cf.source_url == "https://example.com"


def test_competitor_finding_rejects_short_summary() -> None:
    with pytest.raises(ValidationError):
        schemas.CompetitorFinding(**_finding(summary="too short"))


def test_competitor_finding_rejects_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        schemas.CompetitorFinding(**_finding(source_url="ftp://example.com"))


def test_competitor_finding_accepts_http_and_https() -> None:
    schemas.CompetitorFinding(**_finding(source_url="http://example.com"))
    schemas.CompetitorFinding(**_finding(source_url="https://example.com"))


@pytest.mark.parametrize(
    "apology",
    [
        "As an AI I cannot share that, but here are the details " + "x" * 30,
        "I couldn't find anything useful about this competitor " + "x" * 30,
        "I'm unable to access the page but here is what I know " + "x" * 30,
        "I do not have access to live data however " + "x" * 30,
        "No information available about pricing or features " + "x" * 30,
    ],
)
def test_competitor_finding_rejects_apology_patterns(apology: str) -> None:
    with pytest.raises(ValidationError):
        schemas.CompetitorFinding(**_finding(summary=apology))
