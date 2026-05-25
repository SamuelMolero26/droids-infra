"""plan_execution decision layer — pure, stub Anthropic client + stub settings.

These cover the logic that previously diverged between cli.py and tui.py:
step resolution, competitor auto-extraction (the research_team crash fix), and
gmail validation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from droids_agents import execution
from droids_agents.execution import (
    CompetitorsRequired,
    GmailRequired,
    InvalidTaskType,
    plan_execution,
)


class _StubMessages:
    """Returns a fixed text per system-prompt kind. Routes on a substring so a
    single stub can answer classify / plan / extract calls in one test."""

    def __init__(self, *, classify="", plan="", extract="") -> None:
        self._classify = classify
        self._plan = plan
        self._extract = extract

    def create(self, **kw):
        system = kw.get("system", "")
        if "Classify" in system:
            text = self._classify
        elif "Decompose" in system:
            text = self._plan
        elif "Extract" in system:
            text = self._extract
        else:
            text = ""
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


class _StubClient:
    def __init__(self, **kw) -> None:
        self.messages = _StubMessages(**kw)


def _settings(*, gmail_enabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(gmail_enabled=gmail_enabled)


def test_research_with_explicit_competitors_skips_extraction() -> None:
    plan = plan_execution(
        settings=_settings(),
        client=_StubClient(classify="research"),
        prompt="research OpenAI vs Anthropic",
        competitors=["OpenAI", "Anthropic"],
        task_type_override=None,
    )
    assert plan.steps == ["research"]
    assert plan.task_type == "competitor_research"
    assert plan.competitors == ["OpenAI", "Anthropic"]


def test_research_with_empty_competitors_auto_extracts() -> None:
    """The research_team crash fix: empty competitors → extract from prompt."""
    plan = plan_execution(
        settings=_settings(),
        client=_StubClient(
            classify="research",
            extract='{"competitors": ["OpenAI", "Anthropic"]}',
        ),
        prompt="Research API cost between OpenAI and Anthropic",
        competitors=[],
        task_type_override=None,
    )
    assert plan.competitors == ["OpenAI", "Anthropic"]


def test_research_with_unextractable_competitors_raises() -> None:
    with pytest.raises(CompetitorsRequired) as ei:
        plan_execution(
            settings=_settings(),
            client=_StubClient(classify="research", extract='{"competitors": []}'),
            prompt="research the market generally",
            competitors=[],
            task_type_override=None,
        )
    assert ei.value.code == "competitors_required"


def test_non_research_step_does_not_extract_competitors() -> None:
    plan = plan_execution(
        settings=_settings(),
        client=_StubClient(classify="docs"),
        prompt="summarize these docs",
        competitors=[],
        task_type_override=None,
    )
    assert plan.steps == ["docs"]
    assert plan.competitors == []


def test_messaging_without_gmail_raises() -> None:
    with pytest.raises(GmailRequired) as ei:
        plan_execution(
            settings=_settings(gmail_enabled=False),
            client=_StubClient(classify="messaging"),
            prompt="email the team",
            competitors=[],
            task_type_override=None,
        )
    assert ei.value.code == "gmail_not_configured"


def test_messaging_with_gmail_ok() -> None:
    plan = plan_execution(
        settings=_settings(gmail_enabled=True),
        client=_StubClient(classify="messaging"),
        prompt="email the team",
        competitors=[],
        task_type_override=None,
    )
    assert plan.steps == ["messaging"]


def test_task_type_override_short_circuits_classifier() -> None:
    # classify text would route to research, but override wins and no LLM runs.
    plan = plan_execution(
        settings=_settings(),
        client=_StubClient(classify="research"),
        prompt="anything",
        competitors=["X"],
        task_type_override="doc_synthesis",
    )
    assert plan.steps == ["docs"]
    assert plan.task_type == "doc_synthesis"


def test_invalid_task_type_override_raises() -> None:
    with pytest.raises(InvalidTaskType) as ei:
        plan_execution(
            settings=_settings(),
            client=_StubClient(),
            prompt="x",
            competitors=[],
            task_type_override="weird",
        )
    assert ei.value.code == "invalid_task_type"


def test_mixed_prompt_plans_multiple_steps() -> None:
    plan = plan_execution(
        settings=_settings(),
        client=_StubClient(
            classify="mixed",
            plan='{"steps": ["research", "docs"]}',
            extract='{"competitors": ["OpenAI"]}',
        ),
        prompt="research OpenAI then write docs",
        competitors=[],
        task_type_override=None,
    )
    assert plan.steps == ["research", "docs"]
    # research present + no competitors given → auto-extracted
    assert plan.competitors == ["OpenAI"]


def test_roles_for_steps_dedup_and_order() -> None:
    assert execution.roles_for_steps(["research", "docs"]) == [
        "competitor",
        "extractor",
        "synthesizer",
    ]
    # research appears once even if duplicated
    assert execution.roles_for_steps(["research", "research"]) == ["competitor"]
