"""Classifier + mixed_planner with mocked Anthropic SDK."""

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


def _patch_client(monkeypatch, text: str) -> None:
    monkeypatch.setattr(router, "_client", lambda settings: _StubClient(text))


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
def test_classify_prompt_normalises_label(monkeypatch, raw, expected) -> None:
    _patch_client(monkeypatch, raw)
    out = router.classify_prompt("anything", settings=None)
    assert out == expected


def test_classify_prompt_falls_back_to_mixed_on_garbage(monkeypatch) -> None:
    _patch_client(monkeypatch, "I cannot help with that")
    assert router.classify_prompt("x", settings=None) == "mixed"


def test_classify_prompt_falls_back_to_mixed_on_empty(monkeypatch) -> None:
    _patch_client(monkeypatch, "")
    assert router.classify_prompt("x", settings=None) == "mixed"


def test_plan_mixed_steps_extracts_unique_ordered_labels(monkeypatch) -> None:
    _patch_client(monkeypatch, '{"steps":["research","docs","research","messaging"]}')
    out = router.plan_mixed_steps("x", settings=None)
    assert out == ["research", "docs", "messaging"]


def test_plan_mixed_steps_caps_at_four(monkeypatch) -> None:
    _patch_client(
        monkeypatch,
        '{"steps":["research","docs","form","messaging","research"]}',
    )
    out = router.plan_mixed_steps("x", settings=None)
    assert len(out) == 4


def test_plan_mixed_steps_falls_back_on_bad_json(monkeypatch) -> None:
    _patch_client(monkeypatch, "not json")
    assert router.plan_mixed_steps("x", settings=None) == ["research"]


def test_plan_mixed_steps_falls_back_on_empty_steps(monkeypatch) -> None:
    _patch_client(monkeypatch, '{"steps":[]}')
    assert router.plan_mixed_steps("x", settings=None) == ["research"]


def test_plan_mixed_steps_ignores_unknown_labels(monkeypatch) -> None:
    _patch_client(monkeypatch, '{"steps":["research","weird","docs"]}')
    assert router.plan_mixed_steps("x", settings=None) == ["research", "docs"]
