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
