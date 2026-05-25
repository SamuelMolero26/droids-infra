"""Classifier + mixed_planner with injected stub client."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from droids_agents import router


class _StubMessages:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kw):
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


class _StubClient:
    def __init__(self, text: str) -> None:
        self.messages = _StubMessages(text)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("research", "research"),
        ("Docs.", "docs"),
        ("  messaging \n", "messaging"),
        ("FORM", "form"),
        ("mixed", "mixed"),
    ],
)
def test_classify_prompt_normalises_label(raw, expected) -> None:
    out = router.classify_prompt("anything", client=_StubClient(raw))
    assert out == expected


def test_classify_prompt_falls_back_to_mixed_on_garbage() -> None:
    assert router.classify_prompt("x", client=_StubClient("I cannot help with that")) == "mixed"


def test_classify_prompt_falls_back_to_mixed_on_empty() -> None:
    assert router.classify_prompt("x", client=_StubClient("")) == "mixed"


def test_plan_mixed_steps_extracts_unique_ordered_labels() -> None:
    out = router.plan_mixed_steps(
        "x", client=_StubClient('{"steps":["research","docs","research","messaging"]}')
    )
    assert out == ["research", "docs", "messaging"]


def test_plan_mixed_steps_caps_at_four() -> None:
    out = router.plan_mixed_steps(
        "x",
        client=_StubClient('{"steps":["research","docs","form","messaging","research"]}'),
    )
    assert len(out) == 4


def test_plan_mixed_steps_parses_markdown_fenced_json() -> None:
    fenced = '```json\n{"steps": ["research", "docs"]}\n```'
    assert router.plan_mixed_steps("x", client=_StubClient(fenced)) == ["research", "docs"]


def test_plan_mixed_steps_falls_back_on_bad_json() -> None:
    assert router.plan_mixed_steps("x", client=_StubClient("not json")) == ["research"]


def test_plan_mixed_steps_falls_back_on_empty_steps() -> None:
    assert router.plan_mixed_steps("x", client=_StubClient('{"steps":[]}')) == ["research"]


def test_plan_mixed_steps_ignores_unknown_labels() -> None:
    assert router.plan_mixed_steps(
        "x", client=_StubClient('{"steps":["research","weird","docs"]}')
    ) == ["research", "docs"]
