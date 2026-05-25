"""Tier-aware slicing tests."""

from __future__ import annotations

import pytest
from droids_agents.schemas import ContextMemory, ContextResponse
from droids_agents.slicing import slice_for


def _mem(kind: str, tier: str, **kw) -> ContextMemory:
    return ContextMemory(
        id=kw.pop("id", "m1"),
        kind=kind,
        task_type=kw.pop("task_type", "competitor_research"),
        title=kw.pop("title", "t"),
        tier=tier,
        learned=kw.pop("learned", ""),
        snippet=kw.pop("snippet", ""),
    )


@pytest.fixture
def bundle() -> ContextResponse:
    return ContextResponse(
        task_type="competitor_research",
        last_session=_mem(
            "session_summary",
            "always",
            id="m1",
            title="last run",
            learned="prior takeaway",
        ),
        user_rules=[
            _mem(
                "user_rule",
                "always",
                id="r1",
                title="be terse",
                learned="always be terse",
            )
        ],
        browse=[
            _mem(
                "task_pattern",
                "browse",
                id="b1",
                title="anthropic url",
                snippet="https://anthropic.com/pricing",
            ),
            _mem(
                "error_resolution",
                "browse",
                id="b2",
                title="cf challenge",
                snippet="bypass cf via headless+stealth",
            ),
        ],
    )


def test_always_tier_reads_learned(bundle: ContextResponse) -> None:
    out = slice_for("drafter", bundle, "anything")
    assert any("always be terse" in line for line in out)


def test_browse_tier_reads_snippet_not_learned(bundle: ContextResponse) -> None:
    out = slice_for("extractor", bundle, "anything")
    assert any("bypass cf" in line for line in out)


def test_competitor_filters_task_pattern_by_prompt_token(bundle: ContextResponse) -> None:
    matching = slice_for("competitor", bundle, "anthropic pricing")
    assert any("anthropic.com" in line for line in matching)
    nonmatching = slice_for("competitor", bundle, "openai")
    assert not any("anthropic.com" in line for line in nonmatching)


def test_synthesizer_pulls_error_resolution_not_task_pattern(bundle: ContextResponse) -> None:
    out = slice_for("synthesizer", bundle, "anything")
    assert any("bypass cf" in line for line in out)
    assert not any("anthropic.com" in line for line in out)


def test_form_planner_pulls_user_rules_and_task_pattern(bundle: ContextResponse) -> None:
    out = slice_for("form_planner", bundle, "anything")
    assert any("always be terse" in line for line in out)
    assert any("anthropic.com" in line for line in out)


def test_memory_loader_role_returns_empty(bundle: ContextResponse) -> None:
    assert slice_for("memory_loader", bundle, "anything") == []
    assert slice_for("rollup", bundle, "anything") == []


def test_empty_bundle_returns_empty(bundle: ContextResponse) -> None:
    empty = ContextResponse(task_type="competitor_research")
    for role in ("competitor", "extractor", "synthesizer", "drafter", "sender", "form_planner"):
        assert slice_for(role, empty, "x") == []


def test_blank_body_lines_are_dropped() -> None:
    b = ContextResponse(
        task_type="doc_synthesis",
        last_session=_mem("session_summary", "always", learned=""),  # empty body
    )
    assert slice_for("synthesizer", b, "x") == []
